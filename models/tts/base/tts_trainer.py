# Copyright (c) 2023 Amphion.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import json
import os
import random
import shutil
import torch
import time
from abc import abstractmethod
from pathlib import Path
import torch
import torch.nn as nn
from tqdm import tqdm
import re

import accelerate
import json5
import numpy as np
from accelerate.logging import get_logger
from accelerate.utils import ProjectConfiguration
from torch.utils.data import ConcatDataset, DataLoader

from models.base.base_sampler import build_samplers
from models.base.new_trainer import BaseTrainer


class TTSTrainer(BaseTrainer):
    r"""The base trainer for all TTS models. It inherits from BaseTrainer and implements
    ``build_criterion``, ``_build_dataset`` and ``_build_singer_lut`` methods. You can inherit from this
    class, and implement ``_build_model``, ``_forward_step``.
    """

    def __init__(self, args=None, cfg=None):
        self.args = args
        self.cfg = cfg

        cfg.exp_name = args.exp_name

        self._init_accelerator()
        self.accelerator.wait_for_everyone()

        # Init logger
        with self.accelerator.main_process_first():
            self.logger = get_logger(args.exp_name, log_level=args.log_level)

        # Log some info
        self.logger.info("=" * 56)
        self.logger.info("||\t\t" + "New training process started." + "\t\t||")
        self.logger.info("=" * 56)
        self.logger.info("\n")
        self.logger.debug(f"Using {args.log_level.upper()} logging level.")
        self.logger.info(f"Experiment name: {args.exp_name}")
        self.logger.info(f"Experiment directory: {self.exp_dir}")
        self.checkpoint_dir = os.path.join(self.exp_dir, "checkpoint")
        if self.accelerator.is_main_process:
            os.makedirs(self.checkpoint_dir, exist_ok=True)
        self.logger.debug(f"Checkpoint directory: {self.checkpoint_dir}")

        # init counts
        self.batch_count: int = 0
        self.step: int = 0
        self.epoch: int = 0
        self.max_epoch = (
            self.cfg.train.max_epoch if self.cfg.train.max_epoch > 0 else float("inf")
        )
        self.logger.info(
            "Max epoch: {}".format(
                self.max_epoch if self.max_epoch < float("inf") else "Unlimited"
            )
        )

        # Check values
        if self.accelerator.is_main_process:
            self._check_basic_configs()
            # Set runtime configs
            self.save_checkpoint_stride = self.cfg.train.save_checkpoint_stride
            self.checkpoints_path = [
                [] for _ in range(len(self.save_checkpoint_stride))
            ]
            self.keep_last = [
                i if i > 0 else float("inf") for i in self.cfg.train.keep_last
            ]
            self.run_eval = self.cfg.train.run_eval

        # set random seed
        with self.accelerator.main_process_first():
            start = time.monotonic_ns()
            self._set_random_seed(self.cfg.train.random_seed)
            end = time.monotonic_ns()
            self.logger.debug(
                f"Setting random seed done in {(end - start) / 1e6:.2f}ms"
            )
            self.logger.debug(f"Random seed: {self.cfg.train.random_seed}")

        # setup data_loader
        with self.accelerator.main_process_first():
            self.logger.info("Building dataset...")
            start = time.monotonic_ns()
            self.train_dataloader, self.valid_dataloader = self._build_dataloader()
            end = time.monotonic_ns()
            self.logger.info(f"Building dataset done in {(end - start) / 1e6:.2f}ms")

        # setup model
        with self.accelerator.main_process_first():
            self.logger.info("Building model...")
            start = time.monotonic_ns()
            self.model = self._build_model()
            end = time.monotonic_ns()
            self.logger.debug(self.model)
            self.logger.info(f"Building model done in {(end - start) / 1e6:.2f}ms")
            self.logger.info(
                f"Model parameters: {self._count_parameters(self.model)/1e6:.2f}M"
            )

        # optimizer & scheduler
        with self.accelerator.main_process_first():
            self.logger.info("Building optimizer and scheduler...")
            start = time.monotonic_ns()
            self.optimizer = self._build_optimizer()
            self.scheduler = self._build_scheduler()
            end = time.monotonic_ns()
            self.logger.info(
                f"Building optimizer and scheduler done in {(end - start) / 1e6:.2f}ms"
            )

        # accelerate prepare
        self.logger.info("Initializing accelerate...")
        start = time.monotonic_ns()
        (
            self.train_dataloader,
            self.valid_dataloader,
        ) = self.accelerator.prepare(
            self.train_dataloader,
            self.valid_dataloader,
        )

        if isinstance(self.model, dict):
            for key in self.model.keys():
                self.model[key] = self.accelerator.prepare(self.model[key])
        else:
            self.model = self.accelerator.prepare(self.model)

        if isinstance(self.optimizer, dict):
            for key in self.optimizer.keys():
                self.optimizer[key] = self.accelerator.prepare(self.optimizer[key])
        else:
            self.optimizer = self.accelerator.prepare(self.optimizer)

        if isinstance(self.scheduler, dict):
            for key in self.scheduler.keys():
                self.scheduler[key] = self.accelerator.prepare(self.scheduler[key])
        else:
            self.scheduler = self.accelerator.prepare(self.scheduler)

        end = time.monotonic_ns()
        self.logger.info(f"Initializing accelerate done in {(end - start) / 1e6:.2f}ms")

        # create criterion
        with self.accelerator.main_process_first():
            self.logger.info("Building criterion...")
            start = time.monotonic_ns()
            self.criterion = self._build_criterion()
            end = time.monotonic_ns()
            self.logger.info(f"Building criterion done in {(end - start) / 1e6:.2f}ms")

        # TODO: Resume from ckpt need test/debug
        with self.accelerator.main_process_first():
            if args.resume:
                self.logger.info("Resuming from checkpoint...")
                start = time.monotonic_ns()
                self.ckpt_path = self._load_model(self.checkpoint_dir, args.resume_type)
                end = time.monotonic_ns()
                self.logger.info(
                    f"Resuming from checkpoint done in {(end - start) / 1e6:.2f}ms"
                )
                self.checkpoints_path = json.load(
                    open(os.path.join(self.ckpt_path, "ckpts.json"), "r")
                )

            self.checkpoint_dir = os.path.join(self.exp_dir, "checkpoint")
            if self.accelerator.is_main_process:
                os.makedirs(self.checkpoint_dir, exist_ok=True)
            self.logger.debug(f"Checkpoint directory: {self.checkpoint_dir}")

        # save config file path
        self.config_save_path = os.path.join(self.exp_dir, "args.json")

        # For TTS tasks
        if self.cfg.train.multi_speaker_training:
            with self.accelerator.main_process_first():
                self.speakers = self._build_speaker_lut()

        # Only for TTS tasks
        self.task_type = "TTS"
        self.logger.info("Task type: {}".format(self.task_type))

    def _init_accelerator(self):
        self.exp_dir = os.path.join(
            os.path.abspath(self.cfg.log_dir), self.args.exp_name
        )
        project_config = ProjectConfiguration(
            project_dir=self.exp_dir,
            logging_dir=os.path.join(self.exp_dir, "log"),
        )
        self.accelerator = accelerate.Accelerator(
            gradient_accumulation_steps=self.cfg.train.gradient_accumulation_step,
            log_with=self.cfg.train.tracker,
            project_config=project_config,
        )
        if self.accelerator.is_main_process:
            os.makedirs(project_config.project_dir, exist_ok=True)
            os.makedirs(project_config.logging_dir, exist_ok=True)
        with self.accelerator.main_process_first():
            self.accelerator.init_trackers(self.args.exp_name)

    ### Following are methods only for TTS tasks ###
    # TODO: LEGACY CODE, NEED TO BE REFACTORED
    def _build_dataset(self):
        pass

    def build_scaler(self):
        pass

    def _build_criterion():
        pass

    def _build_model(self):
        pass

    def _build_dataloader(self):
        """Build dataloader which merges a series of datasets."""
        # Build dataset instance for each dataset and combine them by ConcatDataset
        Dataset, Collator = self._build_dataset()

        # Build train set
        datasets_list = []
        for dataset in self.cfg.dataset:
            subdataset = Dataset(self.cfg, dataset, is_valid=False)
            datasets_list.append(subdataset)
        train_dataset = ConcatDataset(datasets_list)
        train_collate = Collator(self.cfg)
        _, batch_sampler = build_samplers(train_dataset, self.cfg, self.logger, "train")
        train_loader = DataLoader(
            train_dataset,
            collate_fn=train_collate,
            batch_sampler=batch_sampler,
            num_workers=self.cfg.train.dataloader.num_worker,
            pin_memory=self.cfg.train.dataloader.pin_memory,
        )

        # Build test set
        datasets_list = []
        for dataset in self.cfg.dataset:
            subdataset = Dataset(self.cfg, dataset, is_valid=True)
            datasets_list.append(subdataset)
        valid_dataset = ConcatDataset(datasets_list)
        valid_collate = Collator(self.cfg)
        _, batch_sampler = build_samplers(valid_dataset, self.cfg, self.logger, "valid")
        valid_loader = DataLoader(
            valid_dataset,
            collate_fn=valid_collate,
            batch_sampler=batch_sampler,
            num_workers=self.cfg.train.dataloader.num_worker,
            pin_memory=self.cfg.train.dataloader.pin_memory,
        )
        return train_loader, valid_loader

    def _build_optimizer(self):
        pass

    def _build_scheduler(self):
        pass

    def _load_model(self, checkpoint_dir, checkpoint_path=None, resume_type="resume"):
        """Load model from checkpoint. If a folder is given, it will
        load the latest checkpoint in checkpoint_dir. If a path is given
        it will load the checkpoint specified by checkpoint_path.
        **Only use this method after** ``accelerator.prepare()``.
        """
        if checkpoint_path is None:
            ls = [str(i) for i in Path(checkpoint_dir).glob("*")]
            ls.sort(key=lambda x: int(x.split("_")[-3].split("-")[-1]), reverse=True)
            checkpoint_path = ls[0]
        if resume_type == "resume":
            self.accelerator.load_state(checkpoint_path)
        elif resume_type == "finetune":
            accelerate.load_checkpoint_and_dispatch(
                self.accelerator.unwrap_model(self.model),
                os.path.join(checkpoint_path, "pytorch_model.bin"),
            )
            self.logger.info("Load model weights for finetune SUCCESS!")
        else:
            raise ValueError("Unsupported resume type: {}".format(resume_type))
        self.epoch = int(checkpoint_path.split("_")[-3].split("-")[-1]) + 1
        self.step = int(checkpoint_path.split("_")[-2].split("-")[-1]) + 1
        return checkpoint_path

    ### THIS IS MAIN ENTRY ###
    def train_loop(self):
        r"""Training loop. The public entry of training process."""
        # Wait everyone to prepare before we move on
        self.accelerator.wait_for_everyone()
        # dump config file
        if self.accelerator.is_main_process:
            self._dump_cfg(self.config_save_path)

        # self.optimizer.zero_grad()

        # Wait to ensure good to go
        self.accelerator.wait_for_everyone()
        while self.epoch < self.max_epoch:
            self.logger.info("\n")
            self.logger.info("-" * 32)
            self.logger.info("Epoch {}: ".format(self.epoch))

            # Do training & validating epoch
            train_total_loss, train_losses = self._train_epoch()
            if isinstance(train_losses, dict):
                for key, loss in train_losses.items():
                    self.logger.info("  |- Train/{} Loss: {:.6f}".format(key, loss))
                    self.accelerator.log(
                        {"Epoch/Train {} Loss".format(key): loss},
                        step=self.epoch,
                    )

            valid_total_loss, valid_losses = self._valid_epoch()
            if isinstance(valid_losses, dict):
                for key, loss in valid_losses.items():
                    self.logger.info("  |- Valid/{} Loss: {:.6f}".format(key, loss))
                    self.accelerator.log(
                        {"Epoch/Train {} Loss".format(key): loss},
                        step=self.epoch,
                    )

            self.logger.info("  |- Train/Loss: {:.6f}".format(train_total_loss))
            self.logger.info("  |- Valid/Loss: {:.6f}".format(valid_total_loss))
            self.accelerator.log(
                {
                    "Epoch/Train Loss": train_total_loss,
                    "Epoch/Valid Loss": valid_total_loss,
                },
                step=self.epoch,
            )

            self.accelerator.wait_for_everyone()
            if isinstance(self.scheduler, dict):
                for key in self.scheduler.keys():
                    self.scheduler[key].step()
            else:
                self.scheduler.step()

            # Check if hit save_checkpoint_stride and run_eval
            run_eval = False
            if self.accelerator.is_main_process:
                save_checkpoint = False
                hit_dix = []
                for i, num in enumerate(self.save_checkpoint_stride):
                    if self.epoch % num == 0:
                        save_checkpoint = True
                        hit_dix.append(i)
                        run_eval |= self.run_eval[i]

            self.accelerator.wait_for_everyone()
            if self.accelerator.is_main_process and save_checkpoint:
                path = os.path.join(
                    self.checkpoint_dir,
                    "epoch-{:04d}_step-{:07d}_loss-{:.6f}".format(
                        self.epoch, self.step, train_total_loss
                    ),
                )
                self.accelerator.save_state(path)
                json.dump(
                    self.checkpoints_path,
                    open(os.path.join(path, "ckpts.json"), "w"),
                    ensure_ascii=False,
                    indent=4,
                )

                # Remove old checkpoints
                to_remove = []
                for idx in hit_dix:
                    self.checkpoints_path[idx].append(path)
                    while len(self.checkpoints_path[idx]) > self.keep_last[idx]:
                        to_remove.append((idx, self.checkpoints_path[idx].pop(0)))

                # Search conflicts
                total = set()
                for i in self.checkpoints_path:
                    total |= set(i)
                do_remove = set()
                for idx, path in to_remove[::-1]:
                    if path in total:
                        self.checkpoints_path[idx].insert(0, path)
                    else:
                        do_remove.add(path)

                # Remove old checkpoints
                for path in do_remove:
                    shutil.rmtree(path, ignore_errors=True)
                    self.logger.debug(f"Remove old checkpoint: {path}")

            self.accelerator.wait_for_everyone()
            if run_eval:
                # TODO: run evaluation
                pass

            # Update info for each epoch
            self.epoch += 1

        # Finish training and save final checkpoint
        self.accelerator.wait_for_everyone()
        if self.accelerator.is_main_process:
            self.accelerator.save_state(
                os.path.join(
                    self.checkpoint_dir,
                    "final_epoch-{:04d}_step-{:07d}_loss-{:.6f}".format(
                        self.epoch, self.step, valid_total_loss
                    ),
                )
            )
        self.accelerator.end_training()

    ### Following are methods that can be used directly in child classes ###
    def _train_epoch(self):
        r"""Training epoch. Should return average loss of a batch (sample) over
        one epoch. See ``train_loop`` for usage.
        """
        if isinstance(self.model, dict):
            for key in self.model.keys():
                self.model[key].train()
        else:
            self.model.train()

        epoch_sum_loss: float = 0.0
        epoch_losses: dict = {}
        epoch_step: int = 0
        for batch in tqdm(
            self.train_dataloader,
            desc=f"Training Epoch {self.epoch}",
            unit="batch",
            colour="GREEN",
            leave=False,
            dynamic_ncols=True,
            smoothing=0.04,
            disable=not self.accelerator.is_main_process,
        ):
            # # Put the data to cuda device
            # device = self.accelerator.device
            # for k, v in batch.items():
            #     if isinstance(v, torch.Tensor):
            #         batch[k] = v.to(device)

            # Do training step and BP
            with self.accelerator.accumulate(self.model):
                total_loss, train_losses, training_stats = self._train_step(batch)
            self.batch_count += 1

            # Update info for each step
            # TODO: step means BP counts or batch counts?
            if self.batch_count % self.cfg.train.gradient_accumulation_step == 0:
                epoch_sum_loss += total_loss
                for key, value in train_losses.items():
                    epoch_losses[key] += value

                if isinstance(train_losses, dict):
                    for key, loss in train_losses.items():
                        self.accelerator.log(
                            {"Epoch/Train {} Loss".format(key): loss},
                            step=self.step,
                        )

                self.step += 1
                epoch_step += 1

        self.accelerator.wait_for_everyone()

        epoch_sum_loss = (
            epoch_sum_loss
            / len(self.train_dataloader)
            * self.cfg.train.gradient_accumulation_step
        )

        for key in epoch_losses.keys():
            epoch_losses[key] = (
                epoch_losses[key]
                / len(self.train_dataloader)
                * self.cfg.train.gradient_accumulation_step
            )

        return epoch_sum_loss, epoch_losses

    @torch.inference_mode()
    def _valid_epoch(self):
        r"""Testing epoch. Should return average loss of a batch (sample) over
        one epoch. See ``train_loop`` for usage.
        """
        if isinstance(self.model, dict):
            for key in self.model.keys():
                self.model[key].eval()
        else:
            self.model.eval()

        epoch_sum_loss = 0.0
        epoch_losses = dict()
        for batch in tqdm(
            self.valid_dataloader,
            desc=f"Validating Epoch {self.epoch}",
            unit="batch",
            colour="GREEN",
            leave=False,
            dynamic_ncols=True,
            smoothing=0.04,
            disable=not self.accelerator.is_main_process,
        ):
            total_loss, train_losses, training_stats = self._valid_step(batch)
            epoch_sum_loss += total_loss
            for key, value in train_losses.items():
                for key, value in train_losses.items():
                    if key not in epoch_losses.keys():
                        epoch_losses[key] = value
                    else:
                        epoch_losses[key] += value

        epoch_sum_loss = epoch_sum_loss / len(self.valid_dataloader)
        for key in epoch_losses.keys():
            epoch_losses[key] = epoch_losses[key] / len(self.valid_dataloader)

        self.accelerator.wait_for_everyone()

        return epoch_sum_loss, epoch_losses

    def _train_step(self):
        pass

    def _valid_step(self):
        pass

    def _inference(self):
        pass

    def _set_random_seed(self, seed):
        """Set random seed for all possible random modules."""
        random.seed(seed)
        np.random.seed(seed)
        torch.random.manual_seed(seed)

    def _check_nan(self, loss):
        if torch.any(torch.isnan(loss)):
            self.logger.fatal("Fatal Error: NaN!")
            self.logger.error("loss = {:.6f}".format(loss.item()), in_order=True)

    def _check_basic_configs(self):
        if self.cfg.train.gradient_accumulation_step <= 0:
            self.logger.fatal("Invalid gradient_accumulation_step value!")
            self.logger.error(
                f"Invalid gradient_accumulation_step value: {self.cfg.train.gradient_accumulation_step}. It should be positive."
            )
            self.accelerator.end_training()
            raise ValueError(
                f"Invalid gradient_accumulation_step value: {self.cfg.train.gradient_accumulation_step}. It should be positive."
            )

    @staticmethod
    def _count_parameters(model):
        model_param = 0.0
        if isinstance(model, dict):
            for key, value in model.items():
                model_param += sum(p.numel() for p in model[key].parameters())
        else:
            model_param = sum(p.numel() for p in model.parameters())
        return model_param

    def _dump_cfg(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json5.dump(
            self.cfg,
            open(path, "w"),
            indent=4,
            sort_keys=True,
            ensure_ascii=False,
            quote_keys=True,
        )

    def _is_valid_pattern(self, directory_name):
        directory_name = str(directory_name)
        pattern = r"^epoch-\d{4}_step-\d{7}_loss-\d{1}\.\d{6}"
        return re.match(pattern, directory_name) is not None

    def _check_basic_configs(self):
        if self.cfg.train.gradient_accumulation_step <= 0:
            self.logger.fatal("Invalid gradient_accumulation_step value!")
            self.logger.error(
                f"Invalid gradient_accumulation_step value: {self.cfg.train.gradient_accumulation_step}. It should be positive."
            )
            self.accelerator.end_training()
            raise ValueError(
                f"Invalid gradient_accumulation_step value: {self.cfg.train.gradient_accumulation_step}. It should be positive."
            )

    def _build_speaker_lut(self):
        # combine speakers
        if not os.path.exists(os.path.join(self.exp_dir, self.cfg.preprocess.spk2id)):
            speakers = {}
        else:
            with open(
                os.path.join(self.exp_dir, self.cfg.preprocess.spk2id), "r"
            ) as singer_file:
                speakers = json.load(singer_file)
        for dataset in self.cfg.dataset:
            singer_lut_path = os.path.join(
                self.cfg.preprocess.processed_dir, dataset, self.cfg.preprocess.spk2id
            )
            with open(singer_lut_path, "r") as singer_lut_path:
                singer_lut = json.load(singer_lut_path)
            for singer in singer_lut.keys():
                if singer not in speakers:
                    speakers[singer] = len(speakers)
        with open(
            os.path.join(self.exp_dir, self.cfg.preprocess.spk2id), "w"
        ) as singer_file:
            json.dump(speakers, singer_file, indent=4, ensure_ascii=False)
        print(
            "speakers have been dumped to {}".format(
                os.path.join(self.exp_dir, self.cfg.preprocess.spk2id)
            )
        )
        return speakers
