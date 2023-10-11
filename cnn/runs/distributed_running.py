# Copyright (c) 2021, Parallel Systems Architecture Laboratory (PARSA), EPFL &
# Machine Learning and Optimization Laboratory (MLO), EPFL. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# 3. Neither the name of the PARSA, EPFL & MLO, EPFL
#    nor the names of its contributors may be used to endorse or promote
#    products derived from this software without specific prior written
#    permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# -*- coding: utf-8 -*-
import gc
import time
import math

import torch
import torch.distributed as dist
from torch.autograd import Variable

from cnn.dataset.data import create_dataset
from cnn.utils.log import log, logging_computing, logging_sync, \
    logging_display, logging_load
from cnn.utils.meter import AverageMeter, accuracy, save_checkpoint, \
    define_local_tracker
from cnn.utils.lr import adjust_learning_rate

import random
import numpy as np
import os

def same_seeds(seed):
    os.environ['PYTHONHASHSEED'] = str(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)  # if you are using multi-GPU.
    np.random.seed(seed)  # Numpy module.
    random.seed(seed)  # Python random module.

def load_data(args, input, target, tracker):
    """Load a mini-batch and record the loading time."""
    # get variables.
    start_data_time = time.time()

    device = torch.device(torch.cuda.current_device() if args.device != "cpu" else "cpu")

    input, target = input.to(device), target.to(device)
    input_var, target_var = Variable(input), Variable(target)

    # measure the data loading time
    end_data_time = time.time()
    tracker['data_time'].update(end_data_time - start_data_time)
    tracker['end_data_time'] = end_data_time
    return input, target, input_var, target_var


def init_model(args, model):
    """it might because of MPI, the model for each process is not the same.

    This function is proposed to fix this issue,
    i.e., use the  model (rank=0) as the global model.
    """
    log('init model for process (rank {})'.format(args.cur_rank))
    cur_rank = args.cur_rank
    for param in model.parameters():
        param.data = param.data if cur_rank == 0 else param.data - param.data
        dist.all_reduce(param.data, op=dist.ReduceOp.SUM)


def aggregate_gradients(args, model, optimizer):
    """Aggregate gradients."""
    for ind, param in enumerate(model.parameters()):
        # all reduce.
        dist.all_reduce(param.grad.data, op=dist.ReduceOp.SUM)
        # if or not averge the model.
        if args.avg_model:
            param.grad.data /= args.world_size


def inference(model, criterion, input_var, target_var, target):
    """Inference on the given model and get loss and accuracy."""
    output = model(input_var)
    loss = criterion(output, target_var)
    prec1, prec5 = accuracy(output.data, target, topk=(1, 5))
    return loss, prec1, prec5


def train_and_validate(args, model, criterion, optimizer):
    global current_epoch
    same_seeds(args.manual_seed)
    """The training scheme of Hierarchical Local SGD."""
    # get data loader.
    train_loader, val_loader = create_dataset(args)
    args.num_batches_train = math.ceil(
        1.0 * args.num_train_samples_per_device / args.batch_size)
    args.num_batches_total_train = args.num_batches_train * args.num_epochs
    args.num_warmup_samples = args.num_batches_train * args.lr_warmup_size
    args.num_batches_val = math.ceil(
        1.0 * args.num_val_samples_per_device / args.batch_size)

    # define some parameters for training.
    log('we have {} epochs, {} mini-batches per epoch (batch size:{}).'.format(
        args.num_epochs, args.num_batches_train, args.batch_size))

    # train
    init_model(args, model)
    log('start training and validation.')

    if args.evaluate:
        validate(args, val_loader, model, criterion)
        return

    for epoch in range(args.start_epoch, args.num_epochs + 1):
        args.epoch = epoch
        current_epoch = epoch
        #print(f'current epoch {current_epoch}')

        # train
        do_training(args, train_loader, model, optimizer, criterion)

        # evaluate on validation set.
        if epoch % args.eval_freq == 0:
            do_validate(args, val_loader, model, optimizer, criterion)

        # reshuffle the data.
        if args.reshuffle_per_epoch:
            del train_loader, val_loader
            gc.collect()
            log('reshuffle the dataset.')
            train_loader, val_loader = create_dataset(args)

from numpy import save
from pathlib import Path
import sys

class Hook():
    def __init__(self, module, backward=False):
        if backward==False:
            self.hook = module.register_forward_hook(self.hook_fn)
    def hook_fn(self, module, input, output):
        self.input = input
        self.output = output
        self.module = module
    def close(self):
        self.hook.remove()

def do_training(args, train_loader, model, optimizer, criterion):
    # switch to train mode
    model.train()
    tracker = define_local_tracker()
    tracker['start_load_time'] = time.time()

    for iter, (input, target) in enumerate(train_loader):
        '''
        #SAVE TENSORS
        if (iter % 196) == 0:
            names = []
            _layers = []
            def remove_sequential2(network):
                for name, layer in model.named_modules():
                    if list(layer.children()) == []: # if leaf node, add it to list
                        names.append(name)
                        _layers.append(layer)
            remove_sequential2(model)
            hookF = [Hook(layer) for layer in _layers]
        '''
        
        # update local step.
        logging_load(args, tracker)
        args.local_index += 1

        # adjust learning rate (based on the # of accessed samples)
        if args.lr_decay is not None:
            adjust_learning_rate(args, optimizer)

        # load data
        input, target, input_var, target_var = load_data(
            args, input, target, tracker)

        #print('---------------------forward------')
        # inference and get current performance.
        loss, prec1, prec5 = inference(
            model, criterion, input_var, target_var, target)

        #print('--------------------backward------')
        # compute gradient and do local SGD step.
        optimizer.zero_grad()
        loss.backward()
        
        '''
        #SAVE TENSORS
        if (args.local_index % 196) == 1 and (args.cur_rank == 0):
            #dirname = '/parsadata1/mixedprecision_hbfp/fp32distr/'+ str(args.local_index)
            dirname = '/home/parsa/simla/mixedprecision_hbfp/accuracybooster_tensors/hbfp4/'+str(args.mixed_precision)+'_'+str(args.mixed_tile)+'_'+str(args.mant_bits)+ '/'+ str(args.local_index)
            Path(dirname).mkdir(parents=True, exist_ok=True)

            input_dist = {}
            output_dist = {}
            grad_dist = {}
            weight_dist = {}

            params = list(model.named_parameters())
            for i in range(len(params)):
                param_name = params[i][0]
                if params[i][1].requires_grad and ('weight' in param_name):
                    weight_dist[param_name] = params[i][1]
                    grad_dist[param_name] = params[i][1].grad
                    save(dirname + '/' + param_name + '_weight.npy', params[i][1].cpu().detach().numpy())
                    save(dirname + '/' + param_name + '_grad.npy', params[i][1].grad.cpu().detach().numpy())

            for n,h in zip(names,hookF):
                input_dist[n] = h.input
                output_dist[n] = h.output
                save(dirname + '/' + n + '_input.npy', h.input[0].cpu().detach().numpy())
                save(dirname + '/' + n + '_output.npy', h.output[0].cpu().detach().numpy())
        '''
        
        
        # logging locally.
        logging_computing(args, tracker, loss, prec1, prec5, input)

        # sync and apply gradients.
        aggregate_gradients(args, model, optimizer)
        #print('-------------------optimizer------')
        optimizer.step(apply_lr=True, apply_momentum=True)

        # logging display.
        logging_sync(args, tracker)
        logging_display(args, tracker)

        # try to save memory.
        # torch.cuda.empty_cache()
        # del input, input_var, target, target_var, loss, prec1, prec5
        # gc.collect()
        


def do_validate(args, val_loader, model, optimizer, criterion, save=True):
    """Evaluate the model on the test dataset and save to the checkpoint."""
    # evaluate the model.
    val_prec1, val_prec5 = validate(args, val_loader, model, criterion)

    # remember best prec@1 and save checkpoint.
    is_best = val_prec1 > args.best_prec1
    if is_best:
        args.best_prec1 = val_prec1
        args.best_epoch += [args.epoch]
    log('best accuracy for rank {} at lcoal index {} \
        (best epoch {}, current epoch {}): {}.'.format(
        args.cur_rank, args.local_index,
        args.best_epoch[-1] if len(args.best_epoch) != 0 else '',
        args.epoch, args.best_prec1))

    if False:
        best = int(args.best_epoch[-1]) if len(args.best_epoch) != 0 else 0
        #if int(args.epoch)-best > 10:

    if save and args.cur_rank == 0:
        save_checkpoint(
            {
                'arguments': args,
                'current_epoch': args.epoch,
                'local_index': args.local_index,
                'arch': args.arch,
                'state_dict': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'best_prec1': args.best_prec1,
            },
            is_best, dirname=args.checkpoint_root,
            filename='checkpoint.pth.tar',
            save_all=args.save_all_models)


def validate(args, val_loader, model, criterion):
    """A function for model evaluation."""
    # define stat.
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to evaluation mode
    model.eval()
    device = torch.device(torch.cuda.current_device() if args.device != "cpu" else "cpu")

    log('Do validation.')
    for i, (input, target) in enumerate(val_loader):
        if args.cur_rank == 0:
            print('Validation at batch {}/{}'.format(i, args.num_batches_val))
        input, target = input.to(device), target.to(device)

        with torch.no_grad():
            input_var = Variable(input)
            target_var = Variable(target)

            loss, prec1, prec5 = inference(
                model, criterion, input_var, target_var, target)

        # measure accuracy and record loss
        losses.update(loss.item(), input.size(0))
        top1.update(prec1[0], input.size(0))
        top5.update(prec5[0], input.size(0))

    log('Aggregate val accuracy from different partitions.')
    top1_avg, top5_avg = aggregate_accuracy(top1, top5)

    log('Val at batch: {}. \
         Process: {}. Prec@1: {:.3f} Prec@5: {:.3f}'.format(
        args.local_index, args.cur_rank, top1_avg, top5_avg))
    return top1_avg, top5_avg


def aggregate_accuracy(top1, top5):
    def helper(array):
        array = torch.FloatTensor(array)
        dist.all_reduce(array, op=dist.ReduceOp.SUM)
        return array[0] / array[1]
    top1_avg = helper([top1.sum, top1.count])
    top5_avg = helper([top5.sum, top5.count])
    return top1_avg, top5_avg
