import os
import argparse
import numpy as np

import torch

from torch_em.model import UNETR
from torch_em.loss import DiceBasedDistanceLoss
from torch_em.data import MinInstanceSampler, datasets
from torch_em.transform.label import PerObjectDistanceTransform
from torch_em.transform.raw import normalize_percentile, normalize
from torch_em.data.datasets import get_neurips_cellseg_supervised_loader

import micro_sam.training as sam_training
from micro_sam.util import export_custom_sam_model


def neurips_raw_trafo(raw):
    raw = datasets.neurips_cell_seg.to_rgb(raw)  # ensures 3 channels for the neurips data
    raw = normalize_percentile(raw)
    raw = np.mean(raw, axis=0)
    raw = normalize(raw)
    raw = raw * 255
    return raw


def get_dataloaders(patch_shape, data_path):
    """This returns the neurips cellseg data loaders implemented in torch_em:
    https://github.com/constantinpape/torch-em/blob/main/torch_em/data/datasets/neurips_cell_seg.py
    NOTE: It will not download the NeurIPS CellSeg data automatically (https://neurips22-cellseg.grand-challenge.org/)
    """
    label_transform = PerObjectDistanceTransform(
        distances=True, boundary_distances=True, directed_distances=False, foreground=True, instances=True, min_size=0
    )
    sampler = MinInstanceSampler(min_num_instances=3)
    label_dtype = torch.float32

    train_loader = get_neurips_cellseg_supervised_loader(
        root=data_path, split="train", patch_shape=patch_shape, batch_size=2, raw_transform=neurips_raw_trafo,
        label_transform=label_transform, label_dtype=label_dtype, sampler=sampler, num_workers=16, shuffle=True,
    )

    val_loader = get_neurips_cellseg_supervised_loader(
        root=data_path, split="val", patch_shape=patch_shape, batch_size=1, raw_transform=neurips_raw_trafo,
        label_transform=label_transform, label_dtype=label_dtype, sampler=sampler, num_workers=16, shuffle=True,
    )

    # increasing the sampling attempts for the neurips cellseg dataset
    train_loader.dataset.max_sampling_attempts = 5000
    val_loader.dataset.max_sampling_attempts = 5000

    return train_loader, val_loader


def finetune_neurips_cellseg_root(args):
    """Code for finetuning SAM on NeurIPS CellSeg"""
    # override this (below) if you have some more complex set-up and need to specify the exact gpu
    device = "cuda" if torch.cuda.is_available() else "cpu"

    # training settings:
    model_type = args.model_type
    checkpoint_path = None  # override this to start training from a custom checkpoint
    patch_shape = (512, 512)  # the patch shape for training
    n_objects_per_batch = args.n_objects  # this is the number of objects per batch that will be sampled (default: 25)
    freeze_parts = args.freeze  # override this to freeze different parts of the model

    # get the trainable segment anything model
    model = sam_training.get_trainable_sam_model(
        model_type=model_type,
        device=device,
        checkpoint_path=checkpoint_path,
        freeze=freeze_parts
    )
    model.to(device)

    # let's get the UNETR model for automatic instance segmentation pipeline
    unetr = UNETR(
        backbone="sam",
        encoder=model.sam.image_encoder,
        out_channels=3,
        use_sam_stats=True,
        final_activation="Sigmoid",
        use_skip_connection=False,
        resize_input=True,
        use_conv_transpose=True,
    )
    unetr.to(device)

    # let's get the parameters for SAM and the decoder from UNETR
    joint_model_params = [params for params in model.parameters()]  # sam parameters
    for name, params in unetr.named_parameters():  # unetr's decoder parameters
        if not name.startswith("encoder"):
            joint_model_params.append(params)

    # all the stuff we need for training
    optimizer = torch.optim.Adam(joint_model_params, lr=1e-5)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", factor=0.9, patience=50, verbose=True)
    train_loader, val_loader = get_dataloaders(patch_shape=patch_shape, data_path=args.input_path)

    # this class creates all the training data for a batch (inputs, prompts and labels)
    convert_inputs = sam_training.ConvertToSamInputs(
        transform=model.transform, box_distortion_factor=0.025
    )

    checkpoint_name = f"{args.model_type}/neurips_cellseg_sam"

    # the trainer which performs the joint training and validation (implemented using "torch_em")
    trainer = sam_training.JointSamTrainer(
        name=checkpoint_name,
        save_root=args.save_root,
        train_loader=train_loader,
        val_loader=val_loader,
        model=model,
        optimizer=optimizer,
        device=device,
        lr_scheduler=scheduler,
        logger=sam_training.JointSamLogger,
        log_image_interval=100,
        mixed_precision=True,
        convert_inputs=convert_inputs,
        n_objects_per_batch=n_objects_per_batch,
        n_sub_iteration=8,
        compile_model=False,
        mask_prob=0.5,  # (optional) overwrite to provide the probability of using mask inputs while training
        unetr=unetr,
        instance_loss=DiceBasedDistanceLoss(mask_distances_in_bg=True),
        instance_metric=DiceBasedDistanceLoss(mask_distances_in_bg=True)
    )
    trainer.fit(args.iterations, save_every_kth_epoch=args.save_every_kth_epoch)
    if args.export_path is not None:
        checkpoint_path = os.path.join(
            "" if args.save_root is None else args.save_root, "checkpoints", checkpoint_name, "best.pt"
        )
        export_custom_sam_model(
            checkpoint_path=checkpoint_path,
            model_type=model_type,
            save_path=args.export_path,
        )


def main():
    parser = argparse.ArgumentParser(description="Finetune Segment Anything for the NeurIPS CellSeg dataset.")
    parser.add_argument(
        "--input_path", "-i", default="/scratch/projects/nim00007/sam/data/neurips-cell-seg/",
        help="The filepath to the NeurIPS CellSeg data. If the data does not exist yet it will be downloaded."
    )
    parser.add_argument(
        "--model_type", "-m", default="vit_b",
        help="The model type to use for fine-tuning. Either vit_t, vit_b, vit_l or vit_h."
    )
    parser.add_argument(
        "--save_root", "-s",
        help="Where to save the checkpoint and logs. By default they will be saved where this script is run."
    )
    parser.add_argument(
        "--iterations", type=int, default=int(25e4),
        help="For how many iterations should the model be trained? By default 250k."
    )
    parser.add_argument(
        "--export_path", "-e",
        help="Where to export the finetuned model to. The exported model can be used in the annotation tools."
    )
    parser.add_argument(
        "--freeze", type=str, nargs="+", default=None,
        help="Which parts of the model to freeze for finetuning."
    )
    parser.add_argument(
        "--save_every_kth_epoch", type=int, default=None,
        help="To save every kth epoch while fine-tuning. Expects an integer value."
    )
    parser.add_argument(
        "--n_objects", type=int, default=25, help="The number of instances (objects) per batch used for finetuning."
    )
    args = parser.parse_args()
    finetune_neurips_cellseg_root(args)


if __name__ == "__main__":
    main()