# ------------------------------------------
# IMPORTS
# ------------------------------------------

import torch
from torch import nn
import torch.nn.functional as F

from FastFCN import encoding
from FastFCN.encoding import dilated as resnet
from FastFCN.encoding.utils import batch_pix_accuracy, batch_intersection_union

# ------------------------------------------
# Network Modules
# ------------------------------------------


# ----- Base Network ----- #
UP_KWARGS = {'mode': 'bilinear', 'align_corners': True}

class BaseNet(nn.Module):
    def __init__(self, nclass, backbone, aux, se_loss, jpu=True, dilated=False, norm_layer=None,
                 base_size=520, crop_size=480, mean=[.485, .456, .406],
                 std=[.229, .224, .225], root='~/.encoding/models', **kwargs):
        super(BaseNet, self).__init__()
        self.nclass = nclass
        self.aux = aux
        self.se_loss = se_loss
        self.mean = mean
        self.std = std
        self.base_size = base_size
        self.crop_size = crop_size
        # copying modules from pretrained models
        if backbone == 'resnet50':
            self.pretrained = resnet.resnet50(pretrained=True, dilated=dilated,
                                              norm_layer=norm_layer, root=root)
        elif backbone == 'resnet101':
            self.pretrained = resnet.resnet101(pretrained=True, dilated=dilated,
                                               norm_layer=norm_layer, root=root)
        elif backbone == 'resnet152':
            self.pretrained = resnet.resnet152(pretrained=True, dilated=dilated,
                                               norm_layer=norm_layer, root=root)
        else:
            raise RuntimeError('unknown backbone: {}'.format(backbone))
        # don't keep track of gradients in pretrained model!
        for param in self.pretrained.parameters():
            param.requires_grad = False
        # bilinear upsample options
        self._up_kwargs = UP_KWARGS
        self.backbone = backbone
        self.jpu = JPU([512, 1024, 2048], width=512, norm_layer=norm_layer, up_kwargs=self._up_kwargs) if jpu else None

    def base_forward(self, x):
        x = self.pretrained.conv1(x)
        x = self.pretrained.bn1(x)
        x = self.pretrained.relu(x)
        x = self.pretrained.maxpool(x)
        c1 = self.pretrained.layer1(x)
        c2 = self.pretrained.layer2(c1)
        c3 = self.pretrained.layer3(c2)
        c4 = self.pretrained.layer4(c3)

        if self.jpu:
            return self.jpu(c1, c2, c3, c4)
        else:
            return c1, c2, c3, c4

    def evaluate(self, x, target=None):
        pred = self.forward(x)
        if isinstance(pred, (tuple, list)):
            pred = pred[0]
        if target is None:
            return pred
        correct, labeled = batch_pix_accuracy(pred.data, target.data)
        inter, union = batch_intersection_union(pred.data, target.data, self.nclass)
        return correct, labeled, inter, union

# ----- EncNet Module ----- #
class EncNet(BaseNet):
    def __init__(self, nclass, backbone, aux=True, se_loss=True,
                 norm_layer=nn.BatchNorm2d, **kwargs):
        super(EncNet, self).__init__(nclass, backbone, aux, se_loss,
                                     norm_layer=norm_layer, **kwargs)
        self.head = EncHead([512, 1024, 2048], self.nclass, se_loss=se_loss, jpu=kwargs['jpu'],
                            lateral=kwargs['lateral'], norm_layer=norm_layer,
                            up_kwargs=self._up_kwargs)
        if aux:
            self.auxlayer = FCNHead(1024, nclass, norm_layer=norm_layer)

    def forward(self, x):
        imsize = x.size()[2:]
        features = self.base_forward(x)

        x = list(self.head(*features))
        x[0] = F.interpolate(x[0], imsize, **self._up_kwargs)
        if self.aux:
            auxout = self.auxlayer(features[2])
            auxout = F.interpolate(auxout, imsize, **self._up_kwargs)
            x.append(auxout)
        return tuple(x)


# ----- ENCHEAD ------

class EncHead(nn.Module):
    def __init__(self, in_channels, out_channels, se_loss=True, jpu=True, lateral=False,
                  norm_layer=None, up_kwargs=None):
        super(EncHead, self).__init__()
        self.se_loss = se_loss
        self.lateral = lateral
        self.up_kwargs = up_kwargs
        self.conv5 = nn.Sequential(nn.Conv2d(in_channels[-1], 512, 1, bias=False),
                                    norm_layer(512),
                                    nn.ReLU(inplace=True)) if jpu else \
                      nn.Sequential(nn.Conv2d(in_channels[-1], 512, 3, padding=1, bias=False),
                                    norm_layer(512),
                                    nn.ReLU(inplace=True))
        if lateral:
            self.connect = nn.ModuleList([
                nn.Sequential(
                    nn.Conv2d(in_channels[0], 512, kernel_size=1, bias=False),
                    norm_layer(512),
                    nn.ReLU(inplace=True)),
                nn.Sequential(
                    nn.Conv2d(in_channels[1], 512, kernel_size=1, bias=False),
                    norm_layer(512),
                    nn.ReLU(inplace=True)),
            ])
            self.fusion = nn.Sequential(
                    nn.Conv2d(3*512, 512, kernel_size=3, padding=1, bias=False),
                    norm_layer(512),
                    nn.ReLU(inplace=True))
        self.encmodule = EncModule(512, out_channels, ncodes=32,
            se_loss=se_loss, norm_layer=norm_layer)
        self.conv6 = nn.Sequential(nn.Dropout2d(0.1, False),
                                    nn.Conv2d(512, out_channels, 1))

    def forward(self, *inputs):
        feat = self.conv5(inputs[-1])
        if self.lateral:
            c2 = self.connect[0](inputs[1])
            c3 = self.connect[1](inputs[2])
            feat = self.fusion(torch.cat([feat, c2, c3], 1))
        outs = list(self.encmodule(feat))
        outs[0] = self.conv6(outs[0])
        return tuple(outs)

# ----- FCN Head ----- #
class FCNHead(nn.Module):
    def __init__(self, in_channels, out_channels, norm_layer):
        super(FCNHead, self).__init__()
        inter_channels = in_channels // 4
        self.conv5 = nn.Sequential(nn.Conv2d(in_channels, inter_channels, 3, padding=1, bias=False),
                                   norm_layer(inter_channels),
                                   nn.ReLU(),
                                   nn.Dropout2d(0.1, False),
                                   nn.Conv2d(inter_channels, out_channels, 1))

    def forward(self, x):
        return self.conv5(x)


# ----- Enc Module ----- #

class EncModule(nn.Module):
    def __init__(self, in_channels, nclass, ncodes=32, se_loss=True, norm_layer=None):
        super(EncModule, self).__init__()
        self.se_loss = se_loss
        self.encoding = nn.Sequential(
            nn.Conv2d(in_channels, in_channels, 1, bias=False),
            norm_layer(in_channels),
            nn.ReLU(inplace=True),
            encoding.nn.Encoding(D=in_channels, K=ncodes),
            nn.BatchNorm1d(ncodes),
            nn.ReLU(inplace=True),
            encoding.nn.Mean(dim=1))
        self.fc = nn.Sequential(
            nn.Linear(in_channels, in_channels),
            nn.Sigmoid())
        if self.se_loss:
            self.selayer = nn.Linear(in_channels, nclass)

    def forward(self, x):
        en = self.encoding(x)
        b, c, _, _ = x.size()
        gamma = self.fc(en)
        y = gamma.view(b, c, 1, 1)
        outputs = [F.relu_(x + x * y)]
        if self.se_loss:
            outputs.append(self.selayer(en))
        return tuple(outputs)

# ----- Separable Convolutions ------

class SeparableConv2d(nn.Module):
    def __init__(self, inplanes, planes, kernel_size=3, stride=1, padding=1, dilation=1, bias=False, BatchNorm=nn.BatchNorm2d):
        super(SeparableConv2d, self).__init__()
        self.conv1 = nn.Conv2d(inplanes, inplanes, kernel_size, stride, padding, dilation, groups=inplanes, bias=bias)
        self.bn = BatchNorm(inplanes)
        self.pointwise = nn.Conv2d(inplanes, planes, 1, 1, 0, 1, 1, bias=bias)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn(x)
        x = self.pointwise(x)
        return x


# ----- Joint Pyramid Upsampling (JPU) ------

class JPU(nn.Module):
    def __init__(self, in_channels, width=512, norm_layer=None, up_kwargs=None):
        super(JPU, self).__init__()
        self.up_kwargs = up_kwargs

        self.conv5 = nn.Sequential(
            nn.Conv2d(in_channels[-1], width, 3, padding=1, bias=False),
            norm_layer(width),
            nn.ReLU(inplace=True))
        self.conv4 = nn.Sequential(
            nn.Conv2d(in_channels[-2], width, 3, padding=1, bias=False),
            norm_layer(width),
            nn.ReLU(inplace=True))
        self.conv3 = nn.Sequential(
            nn.Conv2d(in_channels[-3], width, 3, padding=1, bias=False),
            norm_layer(width),
            nn.ReLU(inplace=True))

        self.dilation1 = nn.Sequential(SeparableConv2d(3*width, width, kernel_size=3, padding=1, dilation=1, bias=False),
                                       norm_layer(width),
                                       nn.ReLU(inplace=True))
        self.dilation2 = nn.Sequential(SeparableConv2d(3*width, width, kernel_size=3, padding=2, dilation=2, bias=False),
                                       norm_layer(width),
                                       nn.ReLU(inplace=True))
        self.dilation3 = nn.Sequential(SeparableConv2d(3*width, width, kernel_size=3, padding=4, dilation=4, bias=False),
                                       norm_layer(width),
                                       nn.ReLU(inplace=True))
        self.dilation4 = nn.Sequential(SeparableConv2d(3*width, width, kernel_size=3, padding=8, dilation=8, bias=False),
                                       norm_layer(width),
                                       nn.ReLU(inplace=True))

    def forward(self, *inputs):
        feats = [self.conv5(inputs[-1]), self.conv4(inputs[-2]), self.conv3(inputs[-3])]
        _, _, h, w = feats[-1].size()
        feats[-2] = F.interpolate(feats[-2], (h, w), **self.up_kwargs)
        feats[-3] = F.interpolate(feats[-3], (h, w), **self.up_kwargs)
        feat = torch.cat(feats, dim=1)
        feat = torch.cat([self.dilation1(feat), self.dilation2(feat), self.dilation3(feat), self.dilation4(feat)], dim=1)

        return inputs[0], inputs[1], inputs[2], feat



# ------------------------------------------
# Model Compiler
# ------------------------------------------

def get_model(args):
    ''' Return Modified EncNet / FastFCN, with ResNet backbone.'''

    # UP_KWARGS = {'mode': 'bilinear', 'align_corners': True}

    
    # default settings for epochs, batch_size and lr
    if args.epochs is None:
        epoches = {
            'coco': 30,
            'citys': 240,
            'pascal_voc': 50,
            'pascal_aug': 50,
            'pcontext': 80,
            'ade20k': 120,
        }
        args.epochs = epoches[args.dataset.lower()]
    if args.batch_size is None:
        args.batch_size = 16
    if args.test_batch_size is None:
        args.test_batch_size = args.batch_size
    if args.lr is None:
        lrs = {
            'coco': 0.01,
            'citys': 0.01,
            'pascal_voc': 0.0001,
            'pascal_aug': 0.001,
            'pcontext': 0.001,
            'ade20k': 0.01,
        }
        args.lr = lrs[args.dataset.lower()] / 16 * args.batch_size

    torch.manual_seed(args.seed)
    
    if args.use_lovasz:
        num_class = 1
    else:
        num_class = 2

    return EncNet(num_class, backbone=args.backbone, root='FastFCN/encoding/models',
                        dilated = args.dilated, lateral=args.lateral, jpu=args.jpu, aux=args.aux,
                        se_loss = args.se_loss, norm_layer = nn.BatchNorm2d,
                        base_size = args.base_size, crop_size=args.crop_size)
