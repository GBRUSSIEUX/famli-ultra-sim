import argparse

import math
import os
import pandas as pd
import numpy as np 

import torch
from torch.distributed import is_initialized, get_rank

from loaders.ultrasound_dataset import LotusDataModule
from loaders.mr_us_dataset import VolumeSlicingProbeParamsDataset
from transforms.ultrasound_transforms import LotusEvalTransforms, LotusTrainTransforms
# from callbacks.logger import ImageLoggerLotusNeptune

from nets import lotus
from callbacks import logger

from pytorch_lightning import Trainer
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.strategies.ddp import DDPStrategy

from pytorch_lightning.loggers import NeptuneLogger
# from pytorch_lightning.plugins import MixedPrecisionPlugin

import pickle

import SimpleITK as sitk


def main(args):

    # if(os.path.splitext(args.csv_train_params)[1] == ".csv"):
    #     df_train_params = pd.read_csv(args.csv_train_params)
    #     df_val_params = pd.read_csv(args.csv_valid_params)   
    # else:
    #     df_train_params = pd.read_parquet(args.csv_train_label)
    #     df_val_params = pd.read_parquet(args.csv_valid_label)   

    # if(os.path.splitext(args.csv_train_us)[1] == ".csv"):
    #     df_train_us = pd.read_csv(args.csv_train_us)
    #     df_val_us = pd.read_csv(args.csv_valid_us)   
    # else:
    #     df_train_us = pd.read_parquet(args.csv_train_us)
    #     df_val_us = pd.read_parquet(args.csv_valid_us)   

    if(os.path.splitext(args.csv_train)[1] == ".csv"):
        df_train = pd.read_csv(args.csv_train)
        df_val = pd.read_csv(args.csv_valid)
        df_test = pd.read_csv(args.csv_test)
    else:
        df_train = pd.read_parquet(args.csv_train)
        df_val = pd.read_parquet(args.csv_valid) 
        df_test = pd.read_parquet(args.csv_test)  

    NN = getattr(lotus, args.nn)    
    model = NN(**vars(args))

    if args.init_params:
        df_params = pd.read_csv(args.init_params)
        model.init_params(df_params)

    train_transform = LotusTrainTransforms()
    valid_transform = LotusEvalTransforms()
    lotus_data = LotusDataModule(df_train, df_val, df_test, mount_point=args.mount_point, batch_size=args.batch_size, num_workers=4, img_column="img_path", seg_column="seg_path", train_transform=train_transform, valid_transform=valid_transform, test_transform=valid_transform, drop_last=False)

    checkpoint_callback = ModelCheckpoint(
        dirpath=args.out,
        filename='{epoch}-{val_loss:.2f}',
        save_top_k=2,
        monitor='val_loss'
    )

    early_stop_callback = EarlyStopping(monitor="val_loss", min_delta=0.00, patience=args.patience, verbose=True, mode="min")

    callbacks=[early_stop_callback, checkpoint_callback]
    logger_neptune = None

    if args.neptune_tags:
        logger_neptune = NeptuneLogger(
            project='ImageMindAnalytics/Lotus',
            tags=args.neptune_tags,
            api_key=os.environ['NEPTUNE_API_TOKEN']
        )

        LOGGER = getattr(logger, args.logger)    
        image_logger = LOGGER(log_steps=args.log_steps)
        callbacks.append(image_logger)

    
    trainer = Trainer(
        logger=logger_neptune,
        log_every_n_steps=args.log_steps,
        max_epochs=args.epochs,
        max_steps=args.steps,
        callbacks=callbacks,
        accelerator='gpu', 
        devices=torch.cuda.device_count(),
        strategy=DDPStrategy(find_unused_parameters=False)
        # detect_anomaly=True
    )
    
    trainer.fit(model, datamodule=lotus_data, ckpt_path=args.model)


if __name__ == '__main__':


    parser = argparse.ArgumentParser(description='Diffusion training')

    hparams_group = parser.add_argument_group('Hyperparameters')
    hparams_group.add_argument('--lr', '--learning-rate', default=1e-4, type=float, help='Learning rate')
    hparams_group.add_argument('--epochs', help='Max number of epochs', type=int, default=200)
    hparams_group.add_argument('--patience', help='Max number of patience for early stopping', type=int, default=30)
    hparams_group.add_argument('--steps', help='Max number of steps per epoch', type=int, default=-1)    
    hparams_group.add_argument('--batch_size', help='Batch size', type=int, default=2)
    hparams_group.add_argument('--num_labels', help='Number of labels in the US model', type=int, default=340)
    hparams_group.add_argument('--grid_w', help='Grid size for the simulation', type=int, default=256)
    hparams_group.add_argument('--grid_h', help='Grid size for the simulation', type=int, default=256)
    hparams_group.add_argument('--center_x', help='Position of the circle that creates the transducer', type=float, default=128.0)
    hparams_group.add_argument('--center_y', help='Position of the circle that creates the transducer', type=float, default=-40.0)
    hparams_group.add_argument('--r1', help='Radius of first circle', type=float, default=20.0)
    hparams_group.add_argument('--r2', help='Radius of second circle', type=float, default=224.0)
    hparams_group.add_argument('--theta', help='Aperture angle of transducer', type=float, default=np.pi/4.0)
    hparams_group.add_argument('--alpha_coeff_boundary_map', help='Lotus model', type=float, default=0.1)
    hparams_group.add_argument('--beta_coeff_scattering', help='Lotus model', type=float, default=10)
    hparams_group.add_argument('--tgc', help='Lotus model', type=int, default=8)
    hparams_group.add_argument('--clamp_vals', help='Lotus model', type=int, default=0)
    
    # hparams_group.add_argument('--parceptual_weight', help='Perceptual weight', type=float, default=1.0)
    # hparams_group.add_argument('--adversarial_weight', help='Adversarial weight', type=float, default=1.0)    
    # hparams_group.add_argument('--warm_up_n_epochs', help='Number of warm up epochs before starting to train with discriminator', type=int, default=5)
    
    
    hparams_group.add_argument('--weight_decay', help='Weight decay for optimizer', type=float, default=0.01)
    hparams_group.add_argument('--momentum', help='Momentum for optimizer', type=float, default=0.00)
    hparams_group.add_argument('--kl_weight', help='Weight decay for optimizer', type=float, default=1e-6)    


    input_group = parser.add_argument_group('Input')
    
    input_group.add_argument('--nn', help='Type of neural network', type=str, default="UltrasoundRendering")        
    input_group.add_argument('--model', help='Model to continue training', type=str, default= None)
    input_group.add_argument('--mount_point', help='Dataset mount directory', type=str, default="./")    
    input_group.add_argument('--num_workers', help='Number of workers for loading', type=int, default=4)
    input_group.add_argument('--csv_train', required=True, type=str, help='Train CSV')
    input_group.add_argument('--csv_valid', required=True, type=str, help='Valid CSV')    
    input_group.add_argument('--csv_test', required=True, type=str, help='Test CSV')  
    input_group.add_argument('--img_column', type=str, default='img_path', help='Column name for image')  
    input_group.add_argument('--seg_column', type=str, default='seg_path', help='Column name for labeled/seg image') 
    input_group.add_argument('--init_params', help='Use the dataframe to initialize the mean and std of the diffusor', type=str, default=None)
     
    # input_group.add_argument('--labeled_img', required=True, type=str, help='Labeled volume to grap slices from')    
    # input_group.add_argument('--csv_train_us', required=True, type=str, help='Train CSV')
    # input_group.add_argument('--csv_valid_us', required=True, type=str, help='Valid CSV')    

    output_group = parser.add_argument_group('Output')
    output_group.add_argument('--out', help='Output directory', type=str, default="./")
    
    log_group = parser.add_argument_group('Logging')
    log_group.add_argument('--neptune_tags', help='Neptune tags', type=str, nargs="+", default=None)
    log_group.add_argument('--logger', help='Neptune tags', type=str, nargs="+", default="ImageLoggerLotusNeptune")
    log_group.add_argument('--tb_dir', help='Tensorboard output dir', type=str, default=None)
    log_group.add_argument('--tb_name', help='Tensorboard experiment name', type=str, default="diffusion")
    log_group.add_argument('--log_steps', help='Log every N steps', type=int, default=100)


    args = parser.parse_args()

    main(args)
