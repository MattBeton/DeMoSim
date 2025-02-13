import torch
import torch.distributed as dist
from torch.distributed import init_process_group, destroy_process_group

from torch.optim.lr_scheduler import LambdaLR

from typing import Optional, Callable, Type
import os
from abc import ABC, abstractmethod

from .demo import *

import traceback

class GradientConfig:
    def __init__(self, 
                 optimizer_class: Type[torch.optim.Optimizer] = None, 
                 optimizer_kwargs: dict = None,
                 lr_scheduler: Type[torch.optim.lr_scheduler._LRScheduler] = None,
                 lr_scheduler_kwargs: dict = None,
                 max_local_steps: int = None,
                 **kwargs):
        self.optimizer_class = optimizer_class
        self.optimizer_kwargs = optimizer_kwargs
        self.lr_scheduler = lr_scheduler
        self.lr_scheduler_kwargs = lr_scheduler_kwargs
        self.max_local_steps = max_local_steps

        # Allow additional kwargs to be set as attributes
        for key, value in kwargs.items():
            setattr(self, key, value)

class GradientStrategy:
    def __init__(self, rank, model, config, communication_handler, logger=None):
        self.rank = rank
        self.model = model
        self.config = config
        self.gradient_config = config.gradient_config
        self.communication_handler = communication_handler

        if logger is not None:
            self.logger = logger

        # Initialize scheduler as None; will be set after self.optim is defined in subclasses.
        self.scheduler = None

    def step(self):
        if self.scheduler is not None:
            # print(f'rank {self.rank} scheduler step {self.scheduler.get_last_lr()[0]}')
            # traceback.print_stack()
            self.scheduler.step()
            if self.rank == 0:
                self.logger.log_lr(self.scheduler.get_last_lr()[0])

    def zero_grad(self):
        self.optim.zero_grad()

    def _setup_scheduler(self):
        def lr_lambda(current_step):
            if current_step < self.gradient_config.warmup_steps:
                return float(current_step) / float(max(self.gradient_config.warmup_steps, 1))
            elif self.gradient_config.cosine_anneal:
                min_lr_factor = 0.1
                progress = (current_step - self.gradient_config.warmup_steps) / float(
                    max(1, self.gradient_config.max_local_steps - self.gradient_config.warmup_steps)
                )
                cosine_term = 0.5 * (1.0 + math.cos(math.pi * progress))
                return (1 - min_lr_factor) * cosine_term + min_lr_factor
            else:
                return 1.0
            
        if self.gradient_config.lr_scheduler == 'lambda_cosine':
            self.scheduler = LambdaLR(self.optim, lr_lambda)
        elif self.gradient_config.lr_scheduler is not None:
            lr_sched_kwargs = (self.gradient_config.lr_scheduler_kwargs 
                               if self.gradient_config.lr_scheduler_kwargs is not None else {})
            self.scheduler = self.gradient_config.lr_scheduler(self.optim, **lr_sched_kwargs)
        else:
            self.scheduler = None

class NoCommunicationGradient(GradientStrategy):
    def __init__(self, rank, model, config, communication_handler, logger=None):
        super().__init__(rank, model, config, communication_handler, logger)
        self.optim = self.gradient_config.optimizer_class(model.parameters(), 
                                                          **self.gradient_config.optimizer_kwargs)
        self._setup_scheduler()

    def step(self):
        # No communication.

        self.optim.step()

        super().step()

class FakeAllReduceGradient(GradientStrategy):
    def __init__(self, rank, model, config, communication_handler, logger=None):
        super().__init__(rank, model, config, communication_handler, logger)
        self.optim = self.gradient_config.optimizer_class(model.parameters(), 
                                                          **self.gradient_config.optimizer_kwargs)
        self._setup_scheduler()

    def broadcast_step(self):
        super().step()

        self.optim.step()

        for name, param in self.model.named_parameters():
            if not param.requires_grad:
                continue

            self.communication_handler.post_tensor(name, param.data, self.rank)


    def recv_step(self):
        for name, param in self.model.named_parameters():
            if not param.requires_grad:
                continue

            reduced_data, n = self.communication_handler.all_reduce_tensor(name)
            param.data.copy_(reduced_data / n)


# class SPARTAGradient(GradientStrategy):
#     def __init__(self, rank, model, config, logger=None):
#         super().__init__(rank, model, config, logger)

#         self.optim = self.gradient_config.optimizer_class(model.parameters(), 
#                                                           **self.gradient_config.optimizer_kwargs)
#         self._setup_scheduler()

#         self.index_selector = PartitionedIndexSelector(self.gradient_config.p_sparta)
#         self.index_selector = RandomIndexSelector(self.gradient_config.p_sparta)
#         # self.buffer = []

#     def step(self):
#         with torch.no_grad():
#             for name, param in self.model.named_parameters():
#                 if not param.requires_grad:
#                     continue

#                 indices = self.index_selector.get_indices(param)
#                 dist.broadcast(indices, src=0)
#                 sparse_data = param.data[indices]
#                 dist.all_reduce(sparse_data, op=dist.ReduceOp.SUM)
#                 sparse_data /= dist.get_world_size()

#                 param.masked_scatter_(indices, sparse_data)

#                 # self.buffer.append((indices, sparse_data))
#                 # if len(self.buffer) > self.gradient_config.async_sparta_delay:
#                     # indices_popped, sparse_data_popped = self.buffer.pop(0)
#                     # param.masked_scatter_(indices_popped, sparse_data_popped)


#         self.optim.step()

#         super().step()

# class IndexSelector:
#     def __init__(self, p):
#         self.state = {}
#         self.p = p

#     def get_indices(self, param):
#         return torch.ones(param.shape).bool()


# class RandomIndexSelector(IndexSelector):
#     def get_indices(self, param):
#         return torch.bernoulli(torch.full(param.shape, self.p, device=param.device)).bool()


# class PartitionedIndexSelector(IndexSelector):
#     def __init__(self, p):
#         super().__init__(p)

#     def _set_partition(self, param):
#         param_state = self.state[param]
#         param_state["curr_partition"] = 0
#         param_state["num_partitions"] = min(math.ceil(1 / self.p), param.numel())
#         param_state["partitions"] = (
#             torch.rand(param.numel(), device=param.device).argsort().view(param.shape) % param_state["num_partitions"]
#         )

#     def get_indices(self, param):
#         if param not in self.state:
#             self.state[param] = {}
#             self._set_partition(param)
#         elif self.state[param]["curr_partition"] >= self.state[param]["num_partitions"]:
#             self._set_partition(param)

#         indices = (self.state[param]["partitions"] == self.state[param]["curr_partition"]).bool()

#         self.state[param]["curr_partition"] += 1

#         return indices