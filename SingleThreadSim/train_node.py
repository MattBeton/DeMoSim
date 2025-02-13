import torch
import torch.distributed as dist
from torch.distributed import init_process_group, destroy_process_group
from torch.utils.data import DataLoader, DistributedSampler
import torch.nn.functional as F

from typing import Optional, Callable, Type
import os
import copy

from .sim_config import *
from .gradient_strategy import *
from .wandb_logger import *
from .communication_handler import *

from tqdm import tqdm

class TrainNode:
    '''
    Single node of distributed training process. Should be the same regardless of rank topology/architecture.
    '''
    def __init__(self, 
                 config: SimConfig,
                 device: torch.device,
                 rank: int,
                 logger: WandbLogger,
                 communication_handler: CommunicationHandler,
                 state_dict: Optional[dict] = None):
        self.config = config

        torch.manual_seed(self.config.seed)
        self.logger = logger
        self.device = device
        self.rank = rank
        self.communication_handler = communication_handler

        self.model = self.config.model_class(self.config.gpt_config).to(self.device)
        
        if state_dict is not None:
            self.model.load_state_dict(state_dict)

        self.build_dataloaders()

        self.criterion = self.config.criterion_class(**self.config.criterion_kwargs)

        self.gradient_strategy = self.config.gradient_class(self.rank, 
                                                            self.model, 
                                                            self.config,
                                                            self.communication_handler,
                                                            self.logger)

        self.train_data_iter = iter(self.train_dataloader)
        self.val_data_iter = iter(self.val_dataloader)

    def build_dataloaders(self):
        sampler = DistributedSampler(
            self.config.train_dataset, 
            num_replicas=self.config.num_nodes, 
            rank=self.rank, 
            shuffle=True, 
            drop_last=True
        )

        self.train_dataloader = DataLoader(self.config.train_dataset, 
                          batch_size=self.config.batch_size,
                          sampler=sampler)

        self.val_dataloader = DataLoader(self.config.val_dataset, 
                          batch_size=self.config.batch_size,
                          shuffle=True)

    def save_checkpoint(self, local_step: int):
        if not os.path.exists(os.path.join(self.config.save_dir, self.config.wandb_project, self.logger.wandb_run_id, str(self.rank))):
            os.makedirs(os.path.join(self.config.save_dir, self.config.wandb_project, self.logger.wandb_run_id, str(self.rank)), exist_ok=True)

        filename = f"{local_step}.pt"
        torch.save(self.model.state_dict(), os.path.join(self.config.save_dir, self.config.wandb_project, self.logger.wandb_run_id, str(self.rank), filename))

    def _get_batch(self, eval=False):
        if not eval or self.val_data_iter is None:
            try:
                x, y = next(self.train_data_iter)
            except StopIteration:
                self.epoch += 1
                self.train_data_iter = iter(self.train_dataloader)
                x, y = next(self.train_data_iter)
        else:
            try:
                x, y = next(self.val_data_iter)
            except StopIteration:
                self.val_data_iter = iter(self.val_dataloader)
                x, y = next(self.val_data_iter)

        x, y = x.to(self.device), y.to(self.device)

        return x, y

    def train_step(self):
        x, y = self._get_batch()

        self.gradient_strategy.zero_grad()

        output = self.model(x).transpose(1, 2)
        loss = self.criterion(output, y)
        loss.backward()

        if self.rank == 0:
            self.logger.log_train(loss=loss.item(), rank=self.rank)

        return loss.item()

