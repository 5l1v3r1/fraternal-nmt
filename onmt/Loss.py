"""
This file handles the details of the loss function during training.

This includes: LossComputeBase and the standard NMTLossCompute, and
               sharded loss compute stuff.
"""
from __future__ import division
import torch
import torch.nn as nn
from torch.autograd import Variable
import onmt

class LossComputeBase(nn.Module):
    """
    This is the loss criterion base class. Users can implement their own
    loss computation strategy by making subclass of this one.
    Users need to implement the compute_loss() and make_shard_state() methods.
    We inherits from nn.Module to leverage the cuda behavior.
    """
    def __init__(self, generator, tgt_vocab):
        super(LossComputeBase, self).__init__()
        self.generator = generator
        self.tgt_vocab = tgt_vocab
        self.padding_idx = tgt_vocab.stoi[onmt.IO.PAD_WORD]

    def make_shard_state(self, batch, output, range_, attns=None):
        """
        Make shard state dictionary for shards() to return iterable
        shards for efficient loss computation. Subclass must define
        this method to match its own compute_loss() interface.
        Args:
            batch: the current batch.
            output: the predict output from the model.
            range_: the range of examples for computing, the whole
                    batch or a trunc of it?
            attns: the attns dictionary returned from the model.
        """
        return NotImplementedError

    def compute_loss(self, batch, output, target, **kwargs):
        """
        Compute the loss. Subclass must define this method.
        Args:
            batch: the current batch.
            output: the predict output from the model.
            target: the validate target to compare output with.
            **kwargs(optional): additional info for computing loss.
        """
        return NotImplementedError

    def monolithic_compute_loss(self, batch, output, kappa_output, attns, encoder_output, kappa_encoder_output, decoder_output_wod, kappa_decoder_output_wod):
        """
        Compute the loss monolithically, not dividing into shards.
        """
        range_ = (0, batch.tgt.size(0))
        shard_state = self.make_shard_state(batch, range_, output, kappa_output, encoder_output, kappa_encoder_output, decoder_output_wod, kappa_decoder_output_wod, attns)
        _, batch_stats = self.compute_loss(batch, **shard_state)

        return batch_stats

    def sharded_compute_loss(self, batch, output, kappa_output, attns, encoder_output, kappa_encoder_output, decoder_output_wod, kappa_decoder_output_wod, cur_trunc, trunc_size, shard_size):
        """
        Compute the loss in shards for efficiency.
        """
        batch_stats = onmt.Statistics()

        range_ = (cur_trunc, cur_trunc + trunc_size)

        shard_state = self.make_shard_state(batch, range_, output, kappa_output, encoder_output, kappa_encoder_output, decoder_output_wod, kappa_decoder_output_wod, attns)
        #print(shard_state)
        for shard in shards(shard_state, shard_size):
            loss, stats = self.compute_loss(batch, **shard)
            loss.div(batch.batch_size).backward()
            batch_stats.update(stats)

        return batch_stats

    def stats(self, loss, scores, target):
        """
        Compute and return a Statistics object.

        Args:
            loss(Tensor): the loss computed by the loss criterion.
            scores(Tensor): a sequence of predict output with scores.
        """
        pred = scores.max(1)[1]
        non_padding = target.ne(self.padding_idx)
        num_correct = pred.eq(target) \
                          .masked_select(non_padding) \
                          .sum()
        return onmt.Statistics(loss[0], non_padding.sum(), num_correct)

    def bottle(self, v):
        return v.view(-1, v.size(2))

    def unbottle(self, v, batch_size):
        return v.view(-1, batch_size, v.size(1))


class NMTLossCompute(LossComputeBase):
    """
    Standard NMT Loss Computation.
    """
    def __init__(self, generator, tgt_vocab):
        super(NMTLossCompute, self).__init__(generator, tgt_vocab)

        weight = torch.ones(len(tgt_vocab))
        weight[self.padding_idx] = 0
        self.criterion = nn.NLLLoss(weight, size_average=False)

    def make_shard_state(self, batch, range_, output, attns=None):
        """ See base class for args description. """
        return {
            "output": output,
            "target": batch.tgt[range_[0] + 1: range_[1]],
        }

    def compute_loss(self, batch, output, target):
        """ See base class for args description. """
        scores = self.generator(self.bottle(output))
        scores_data = scores.data.clone()

        target = target.view(-1)
        target_data = target.data.clone()

        loss = self.criterion(scores, target)
        loss_data = loss.data.clone()

        stats = self.stats(loss_data, scores_data, target_data)

        return loss, stats

class NMTKappaLossCompute(LossComputeBase):
    """
    New NMT Kappa L2 Loss Computation.
    """
    def __init__(self, generator, tgt_vocab, kappa_enc, kappa_dec):
        super(NMTKappaLossCompute, self).__init__(generator, tgt_vocab)

        weight = torch.ones(len(tgt_vocab))
        weight[self.padding_idx] = 0
        self.criterion = nn.NLLLoss(weight, size_average=False)
        self.kappa_enc = kappa_enc
        self.kappa_dec = kappa_dec
        # adjusted this to cross entropy loss
        #self.criterion = nn.CrossEntropyLoss()

    def make_shard_state(self, batch, range_, output, kappa_output, encoder_output, kappa_encoder_output, decoder_output_wod, kappa_decoder_output_wod, attns=None):
        """ See base class for args description. """
        return {
            "output": output,
            "kappa_output": kappa_output,
            "encoder_output": encoder_output,
            "kappa_encoder_output": kappa_encoder_output,
            "decoder_output_wod": decoder_output_wod,
            "kappa_decoder_output_wod": kappa_decoder_output_wod,
            "target": batch.tgt[range_[0] + 1: range_[1]],
        }

    def compute_loss(self, batch, target, output, kappa_output, encoder_output, kappa_encoder_output, decoder_output_wod, kappa_decoder_output_wod):
        """ See base class for args description. """

        # Normal output
        scores = self.generator(self.bottle(output))
        scores_data = scores.data.clone()

        # Kappa output
        kappa_scores = self.generator(self.bottle(kappa_output))
        kappa_scores_data = kappa_scores.data.clone()

        # target
        target = target.view(-1)
        target_data = target.data.clone()

        raw_loss = self.criterion(scores, target)
        # compute loss data
        loss_data = raw_loss.data.clone()
        loss = raw_loss

        if self.kappa_enc is not None and self.kappa_dec is not None:
            # we adjust the the loss to the Kappa output
            loss += self.criterion(kappa_scores, target)
            loss = loss/2
            l2_kappa_decoder = (decoder_output_wod - kappa_decoder_output_wod).pow(2).mean()
            l2_kappa_encoder = (encoder_output - kappa_encoder_output).pow(2).mean()
            loss += (self.kappa_dec * l2_kappa_decoder) + (self.kappa_enc * l2_kappa_encoder)

            # compute loss statistics
            stats = self.stats(loss_data, kappa_scores_data, target_data)
        else:
            # we return normal loss here
            stats = self.stats(loss_data, scores_data, target_data)

        return loss, stats

def filter_shard_state(state):
    for k, v in state.items():
        if v is not None:
            if isinstance(v, Variable) and v.requires_grad:
                v = Variable(v.data, requires_grad=True, volatile=False)
            yield k, v


def shards(state, shard_size, eval=False):
    """
    Args:
        state: A dictionary which corresponds to the output of
               *LossCompute.make_shard_state(). The values for
               those keys are Tensor-like or None.
        shard_size: The maximum size of the shards yielded by the model.
        eval: If True, only yield the state, nothing else.
              Otherwise, yield shards.

    Yields:
        Each yielded shard is a dict.

    Side effect:
        After the last shard, this function does back-propagation.
    """
    if eval:
        yield state
    else:
        # non_none: the subdict of the state dictionary where the values
        # are not None.
        non_none = dict(filter_shard_state(state))

        # Now, the iteration:
        # state is a dictionary of sequences of tensor-like but we
        # want a sequence of dictionaries of tensors.
        # First, unzip the dictionary into a sequence of keys and a
        # sequence of tensor-like sequences.
        keys, values = zip(*((k, torch.split(v, shard_size))
                             for k, v in non_none.items()))

        # Now, yield a dictionary for each shard. The keys are always
        # the same. values is a sequence of length #keys where each
        # element is a sequence of length #shards. We want to iterate
        # over the shards, not over the keys: therefore, the values need
        # to be re-zipped by shard and then each shard can be paired
        # with the keys.
        for shard_tensors in zip(*values):
            yield dict(zip(keys, shard_tensors))

        # Assumed backprop'd
        variables = ((state[k], v.grad.data) for k, v in non_none.items()
                     if isinstance(v, Variable) and v.grad is not None)
        inputs, grads = zip(*variables)
        torch.autograd.backward(inputs, grads)
