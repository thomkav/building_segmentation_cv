'''
# ------------------------------------------
# ------------------------------------------
# Train Modified FastFCN
# ------------------------------------------
# ------------------------------------------
'''

import argparse
import os
import numpy as np
import torch
import pipeline.criterion as Criteron
from pipeline.load import get_dataloader
import pipeline.network as Network
from datetime import datetime, timedelta



class ObjectView:
    '''
    Helper class to access dict values as attributes.

    Replaces command-line arg-parse options.
    '''
    def __init__(self, d):
        self.__dict__ = d


def save_model(model, experiment_name=None):
    '''
    Save a model to the correct dictionary
    '''

    model_dir = os.path.join('models', experiment_name)
    if not os.path.exists(model_dir):
        os.makedirs(model_dir)

    model_path = os.path.join(model_dir, '{}_m.pt'.format(experiment_name))
    torch.save(model.state_dict(), model_path)

    return None


class EarlyStopping:
    """Early stops the training if validation loss doesn't improve after a given patience."""
    def __init__(self, patience=7, verbose=False, delta=0):
        """
        Args:
            patience (int): How long to wait after last time validation loss improved.
                            Default: 7
            verbose (bool): If True, prints a message for each validation loss improvement. 
                            Default: False
            delta (float): Minimum change in the monitored quantity to qualify as an improvement.
                            Default: 0
        """
        self.patience = patience
        self.verbose = verbose
        self.counter = 0
        self.best_score = None
        self.early_stop = False
        self.val_loss_min = np.Inf
        self.delta = delta

    def __call__(self, val_loss, model, experiment_name):

        score = -val_loss

        if self.best_score is None:
            self.best_score = score
            self.save_checkpoint(val_loss, model, experiment_name)
        elif score < self.best_score + self.delta:
            self.counter += 1
            print(f'EarlyStopping counter: {self.counter} out of {self.patience}')
            if self.counter >= self.patience:
                self.early_stop = True
        else:
            self.best_score = score
            self.save_checkpoint(val_loss, model, experiment_name)
            self.counter = 0

    def save_checkpoint(self, val_loss, model, experiment_name):
        '''Saves model when validation loss decrease.'''
        if self.verbose:
            print(f'Validation loss decreased ({self.val_loss_min:.6f} --> {val_loss:.6f}).  Saving model ...')
        save_model(model, experiment_name=experiment_name + '_chkpt')
        self.val_loss_min = val_loss


def train_fastfcn_mod(
    options=None, num_epochs=1, reporting_int=5, batch_size=8,
    experiment_name=None, train_path=None, batch_trim=None
    ):
    '''
    Compile and train the modified FastFCN implementation.
    '''

    model_prefix = (datetime.now()-timedelta(hours=5)).strftime("%d-%m-%Y_%H-%M")
    if experiment_name is not None:
        experiment_name = model_prefix + "__" + experiment_name
    else:
        experiment_name = model_prefix

    if options is None:
        options = {
            'model': 'encnet', # model name (default: encnet)
            'backbone': 'resnet50', # backbone name (default: resnet50)
            'jpu': True, # 'JPU'
            'dilated': False, # 'dilation'
            'lateral': False, #'employ FPN')
            'dataset':'ade20k', # 'dataset name (default: pascal12)')
            'workers': 16, # dataloader threads
            'base_size': 520, # 'base image size'
            'crop_size': 480, # 'crop image size')
            'train_split':'train', # 'dataset train split (default: train)'

            # training hyper params
            'aux': True, # 'Auxilary Loss'
            'aux_weight': 0.2, # 'Auxilary loss weight (default: 0.2)'
            'se_loss': True, # 'Semantic Encoding Loss SE-loss'
            'se_weight': 0.2, # 'SE-loss weight (default: 0.2)'
            'epochs': None, # 'number of epochs to train (default: auto)'
            'start_epoch': 0, # 'start epochs (default:0)'
            'batch_size': batch_size, # 'input batch size for training (default: auto)'
            'test_batch_size': None, # 'input batch size for testing (default: same as batch size)'

            # optimizer params
            'lr': None, # 'learning rate (default: auto)'
            'lr_scheduler': 'poly', # 'learning rate scheduler (default: poly)'
            'momentum': 0.9, # 'momentum (default: 0.9)'
            'weight_decay': 1e-4, # 'w-decay (default: 1e-4)'

            # cuda, seed and logging
            'no_cuda': False, # 'disables CUDA training'
            'seed': 1, # 'random seed (default: 1)'

            # checking point
            'resume': None, # 'put the path to resuming file if needed'
            'checkname': 'default', # 'set the checkpoint name'
            'model-zoo': None, # 'evaluating on model zoo model'

            # finetuning pre-trained models
            'ft': False, # 'finetuning on a different dataset'

            # evaluation option
            'split': 'val',
            'mode': 'testval',
            'ms': False, # 'multi scale & flip'
            'no_val': False, # 'skip validation during training'
        }

    options['cuda'] = torch.cuda.is_available() and not options['no_cuda']

    # Convert options dict to attributed object
    model_args = ObjectView(options)
    
    train_dataloader = get_dataloader(
        in_dir=train_path, load_test=False, batch_size=batch_size, batch_trim=batch_trim, split=None
        )
    # train_dataloader = get_dataloader(
    #     path=train_path, load_test=False, batch_size=batch_size, batch_trim=batch_trim, split='train'
    #     )
    # val_dataloader = get_dataloader(
    #     path=train_path, load_test=False, batch_size=batch_size, batch_trim=batch_trim, split='test'
    #     )

    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
    # Compile modified FastFCN model.
    model = Network.get_model(model_args)
    model.to(device)

    # Optimizer (Adam)
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.Adam(params, lr=0.005, weight_decay=0.0005)

    # Larning rate scheduler
    lr_scheduler = torch.optim.lr_scheduler.StepLR(
        optimizer, step_size=3, gamma=0.1
        )

    # Loss Function (Segmentation Loss)
    criterion = Criteron.SegmentationLosses(
        se_loss=model_args.se_loss, aux=model_args.aux, nclass=2,
        se_weight=model_args.se_weight, aux_weight=model_args.aux_weight
        )

    # # Loss Function (Lovasz Hinge)
    # criterion = Criteron.SegmentationLosses(
    #     se_loss=args.se_loss, aux=args.aux, nclass=2,
    #     se_weight=args.se_weight, aux_weight=args.aux_weight
    #     )

    # early_stopper = EarlyStopping(patience=7, verbose=True)

    for epoch in range(num_epochs):  # loop over the dataset multiple times

        train_loss = 0.0

        for i, (images, targets, img_names) in enumerate(train_dataloader, 0):
            images = images.to(device)
            targets = targets.to(device).squeeze(1).round().long()

            # get the inputs; data is a list of [inputs, labels]
            targets.requires_grad = False
            
            # zero the parameter gradients
            optimizer.zero_grad()

            # forward + backward + optimize
            outputs = model(images)
            loss = criterion(*outputs, targets)
            loss.backward()
            optimizer.step()

            # print statistics
            train_loss += loss.item()

            if i % reporting_int == 0:    # print every 2000 mini-batches
                print('[%d, %5d] loss: %.3f' %
                    (epoch + 1, i + 1, train_loss / reporting_int))
                train_loss = 0.0

        # Calculation Validation Loss

        # val_loss = 0
        # for i, (images, masks, img_names) in enumerate(val_dataloader):
        #     images = images.to(device)
        #     targets = targets.to(device).squeeze(1).round().long()

        #     # get the inputs; data is a list of [inputs, labels]
        #     images.requires_grad=False
        #     targets.requires_grad=False

        #     outputs = model(images)
        #     loss = criterion(*outputs, targets)
        #     val_loss += loss.item()
        
        # early_stopper(val_loss, model=model, experiment_name=experiment_name)
            
        # --- end of data iteration -------

        # Check for early stopping conditions:
        # early_stopper(running_loss, model, experiment_name)

        lr_scheduler.step()

        # --- end of epoch -------

    save_model(model, experiment_name)

    return None


if __name__=='__main__':

    PARSER = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    SUBPARSERS = PARSER.add_subparsers(dest='command')

    TRAIN_PARSER = SUBPARSERS.add_parser('all', help=train_fastfcn_mod.__doc__)
    TRAIN_PARSER.add_argument(
        '-name', default=None, type=str, required=False,
        help='Experiment name.')
    TRAIN_PARSER.add_argument(
        '-epochs', default=2, type=int, required=False,
        help='Number of epochs.')
    TRAIN_PARSER.add_argument(
        '-report', default=5, type=int, required=False,
        help='Number of batches between loss reports (int).')
    TRAIN_PARSER.add_argument(
        '-batch_size', default=16, type=int, required=False,
        help='The filter used to match logs.')
    TRAIN_PARSER.add_argument(
        '-train_path', default=None, type=str, required=False,
        help='Folder containing training images, with images and masks subdirectory.')
    TRAIN_PARSER.add_argument(
        '-batch_trim', default=None, type=int, required=False,
        help='Option to only train for a limit number of batches in each epoch.')

    PARSED_ARGS = PARSER.parse_args()
    print('Args:\n', PARSED_ARGS)

    if PARSED_ARGS.command == 'all':
        train_fastfcn_mod(
            num_epochs=PARSED_ARGS.epochs, reporting_int=PARSED_ARGS.report,
            batch_size=PARSED_ARGS.batch_size, experiment_name=PARSED_ARGS.name,
            train_path=PARSED_ARGS.train_path, batch_trim=PARSED_ARGS.batch_trim
            )