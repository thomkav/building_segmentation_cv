

import glob
import os
import re
import numpy as np
import pdb
import torch
import torchvision.transforms as transforms
from torch.utils.data import Dataset, DataLoader
from PIL import Image

colorjitter = transforms.ColorJitter(brightness=0.25, contrast=0.25, saturation=0.25, hue=0.25)
# ---- Image Utitilies ----

def train_transform(image, mask):
    '''
    Custom Pytorch randomized preprocessing of training image and mask.
    '''
    image = transforms.functional.pad(image, padding=0, padding_mode='reflect')
    crop_loc = np.random.randint(0, 728, 2)
    image = transforms.functional.crop(image, *crop_loc, 400, 400)
    mask = transforms.functional.crop(mask, *crop_loc, 400, 400)
    image = colorjitter(image)
    image = transforms.functional.to_tensor(image)
    mask = transforms.functional.to_tensor(mask)
    return image, mask

def val_transform(image, mask):
    image = transforms.functional.center_crop(image, 400)
    mask = transforms.functional.center_crop(mask, 400)
    image = transforms.functional.to_tensor(image)
    mask = transforms.functional.to_tensor(mask)
    return image, mask

# ---- Dataset Class ----
class DatasetWrapper(Dataset):
    def __init__(self, subset, transform=None):
        self.subset = subset
        self.transform = transform
        
    def __getitem__(self, index):
        x, y, z = self.subset[index]
        if self.transform:
            x,y = self.transform(x,y)
        return x, y, z
        
    def __len__(self):
        return len(self.subset)

class MyDataset(Dataset):
    '''
    Custom PyTorch Dataset class.
    '''
    def __init__(self, in_dir=None, custom_transforms=None, load_test=False, split=None, batch_trim=False):

        self.transforms = custom_transforms
        self.load_test = load_test

        if in_dir is None:
            in_dir = 'training_data'
        
        if self.load_test:
            print('Loading Test')
            self.path = 'submission_data/test'
            self.images = glob.glob(os.path.join(self.path, '*'))
            self.images = [os.path.basename(g) for g in self.images]
        else:
            self.path = in_dir
            print(self.path)
            pattern = os.path.join(self.path, 'images', '*.jpg')
            print(pattern)
            self.images = glob.glob(pattern)
            self.basenames = [os.path.basename(g) for g in self.images]
            self.images = []
            self.masks = []
            for basename in self.basenames:
                img = os.path.join(self.path, 'images', basename)
                mask = os.path.join(self.path, 'masks', basename.replace('_i.jpg','_mask.jpg'))
                if (os.path.exists(img) and os.path.exists(mask)):
                    self.images.append(img)
                    self.masks.append(mask)
            

            #if split is not None:
            #    self.images, self.masks = train_test_split(self.images, self.masks, split=split)
                # pdb.set_trace()

            self.coordinates = None
        
        # Option to dataset for speedy development training to subset of batches
        if batch_trim:
            if batch_trim * 16 > len(self.images):
                pass
            else:
                self.images, self.masks = self.images[:int(batch_trim)*16], self.masks[:int(batch_trim)*16]

    def __getitem__(self, index):
        # print(index)
        if self.load_test:
            img_name = self.images[index]
            image = Image.open(os.path.join(self.path, img_name, img_name + '.tif'))
            image_tensor = transforms.functional.to_tensor(image)[:3]
            return image_tensor, img_name
        else:
            image = Image.open(self.images[index])
            mask = Image.open(self.masks[index])
            img_name = self.images[index]
        if self.transforms is not None:
            image, mask = self.transforms(image, mask)
        return (image, mask, img_name)

    def __len__(self):
        return len(self.images)


# ---- Load Dataset ----

def get_dataloader(in_dir=None, load_test=False, batch_size=16, batch_trim=False, overwrite=False, out_dir=None, split=None):
    '''
    Load pytorch batch data loader only
    '''

    def filter_written(name):
        img_path = '{}/{}.tif'.format(out_dir, name)
        if os.path.exists(img_path):
            return False
        else:
            return True
    
    print('Load test:', load_test)
    dataset = MyDataset(
        in_dir=in_dir, custom_transforms=None,
        load_test=load_test, batch_trim=batch_trim, split=split
        )

    # Check if images have been written.

    if overwrite:
        # Don't filter 
        pass
    else:
        print('Filtering images already written.')        
        dataset.images = list(filter(filter_written, dataset.images))
        if len(dataset.images)==0:
            print('All images already predicted')
            return False
    if split=='random':
        train_len = int(len(dataset)*0.8)
        lengths = [train_len, len(dataset)-train_len]
        train_subset, val_subset = torch.utils.data.random_split(dataset, lengths)
        val_dataset = DatasetWrapper(val_subset, transform=val_transform)
        train_dataset = DatasetWrapper(train_subset, transform=train_transform)
        train_loader = DataLoader(train_dataset, shuffle=True, batch_size=batch_size, pin_memory=True,num_workers=3)
        val_loader = DataLoader(val_dataset, shuffle=False, batch_size=batch_size//4, pin_memory=True, num_workers=3)
        return train_loader, val_loader

    print('Dataset Loaded.')

    batch_loader = DataLoader(
            dataset, shuffle=True, batch_size=batch_size, pin_memory=True
            )

    return batch_loader



# ---- Train Test Split ----

def train_test_split(images, masks, split, tier=1):
    '''
    Filter images according to their train / test split.
    '''

    # 6 of 7 cities used for train region
    # Training regs are 86.0% of total area
    # Training regs are 65.0% of unique blocks
    # Train scene ID list (tier 1): ['f883a0' '4e7c7f' 'f15272' '825a50' 'f49f31' 'e52478']
    # Test scene ID list (tier 1): ['d41d81']

    if split == 'train':
        regions = {
            'f883a0': {
                'skip_region': [
                    # (x_0, x_1 , y_0, y_1), # x lim, y lim
                    # (x_0, x_1 , y_0, y_1), # x lim, y lim
                ]
            },
            '4e7c7f': {
                'skip_region': [
                    # (x_0, x_1 , y_0, y_1), # x lim, y lim
                    # (x_0, x_1 , y_0, y_1), # x lim, y lim
                ]
            },
            'f15272': {
                'skip_region': [
                    # (x_0, x_1 , y_0, y_1), # x lim, y lim
                    # (x_0, x_1 , y_0, y_1), # x lim, y lim
                ]
            },
            '825a50': {
                'skip_region': [
                    # (x_0, x_1 , y_0, y_1), # x lim, y lim
                    # (x_0, x_1 , y_0, y_1), # x lim, y lim
                ]
            },
            'f49f31': {
                'skip_region': [
                    # (x_0, x_1 , y_0, y_1), # x lim, y lim
                    # (x_0, x_1 , y_0, y_1), # x lim, y lim
                ]
            },
            'e52478': {
                'skip_region': [
                    # (x_0, x_1 , y_0, y_1), # x lim, y lim
                    # (x_0, x_1 , y_0, y_1), # x lim, y lim
                ]
            }
        }
    elif split == 'test':
        regions = {
            'd41d81': {
                'skip_region': [
                    # (x_0, x_1 , y_0, y_1), # x lim, y lim
                    # (x_0, x_1 , y_0, y_1), # x lim, y lim
                ]
            }
        }


    def filter_regions(filename):
        '''
        Filter filenames according to allowed regions.
        '''

        filename = os.path.basename(filename)
        _, x_pos, y_pos, _ = filename.split('_')
    
        for key, values in regions.items():
            
            # Check all matching regions.
            if re.search(key, filename):
                # Skip regions that are badly labeled.
                for x_0, x_1, y_0, y_1 in values['skip_region']:
                    # print(x_0, x_1, y_0, y_1, "___", x_pos, y_pos)
                    if x_0 <= int(x_pos) <= x_1:
                        return False
                    if y_0 <= int(y_pos) <= y_1:
                        return False
                return True
        return False

    #filtered_imgs = list(filter(filter_regions, images))
    #filtered_masks = list(filter(filter_regions, masks))

    #assert len(filtered_imgs) == len(filtered_masks)

    #pdb.set_trace()
    #return filtered_imgs, filtered_masks
