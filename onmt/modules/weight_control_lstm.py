# This is the

import torch
import torch.nn as nn
from torch.nn import Parameter
from functools import wraps
import math


class WeightDrop(torch.nn.Module):
    def __init__(self, module, weights, dropout=0,):
        """
        :param module: a LSTM module
        :param weights:
        :param dropout:
        :param n_languages:
        :param rank:
        """
        super(WeightDrop, self).__init__()
        self.module = module
        self.weights = weights
        self.dropout = dropout
        self._setup()

    def trails_in_the_sky(*args, **kwargs):
        # We need to replace flatten_parameters with a nothing function
        # It must be a function rather than a lambda as otherwise pickling explodes
        # We can't write boring code though, so ... TRAILS IN THE SKY ftw!!
        # (╯°□°）╯︵ ┻━┻
        return

    def _setup(self):
        # Terrible temporary solution to an issue regarding compacting weights re: CUDNN RNN
        if issubclass(type(self.module), torch.nn.RNNBase):
            self.module.flatten_parameters = self.trails_in_the_sky
        print(self.weights)

        for name_w in self.weights:
            print('Applying weight drop of {} to {}'.format(self.dropout, name_w))
            w = getattr(self.module, name_w)
            # del self.module._parameters[name_w]  # don't delete so we can load :))
            self.module.register_parameter(name_w + '_raw', Parameter(w.data))

    def _setweights(self):
        for name_w in self.weights:
            raw_w = getattr(self.module, name_w + '_raw')
            w = torch.nn.functional.dropout(raw_w, p=self.dropout, training=self.training)
            setattr(self.module, name_w, Parameter(w))

    def forward(self, *args, **kwargs):
        self._setweights()
        return self.module.forward(*args, **kwargs)


class WeightFactoredLSTM(torch.nn.Module):
    def __init__(self, module, dropout=0, n_languages=1, rank=1, multiplicative=False, activation='none'):
        """
        :param module: a LSTM module
        :param weights:
        :param dropout:
        :param n_languages:
        :param rank:
        """
        super(WeightFactoredLSTM, self).__init__()
        self.module = module
        self.weights = None
        self.dropout = dropout
        self.n_languages = n_languages
        self.rank = rank
        self.multiplicative = multiplicative
        self.activation = activation
        self._setup()

    def trails_in_the_sky(*args, **kwargs):
        # We need to replace flatten_parameters with a nothing function
        # It must be a function rather than a lambda as otherwise pickling explodes
        # We can't write boring code though, so ... TRAILS IN THE SKY ftw!!
        # (╯°□°）╯︵ ┻━┻
        return

    def _setup(self):
        # Terrible temporary solution to an issue regarding compacting weights re: CUDNN RNN
        if issubclass(type(self.module), torch.nn.RNNBase):
            self.module.flatten_parameters = self.trails_in_the_sky
            self.weights = list()
            for l in range(self.module.num_layers):
                self.weights.append("weight_ih_l%d" % l)
                self.weights.append("weight_hh_l%d" % l)
        else:
            # this code only supports nn.LSTM
            raise NotImplementedError

        # In this part: we need to look at two things:
        # First, __setattr__ of a module is overwritten so that the parameter is registered in module._parameter
        # So we need to delete

        for name_w in self.weights:
            print('Applying weight drop of {} to {}'.format(self.dropout, name_w))
            w = getattr(self.module, name_w)
            del self.module._parameters[name_w]
            self.module.register_parameter(name_w + '_raw', Parameter(w.data))

            # for each parameter we need to add two auxiliary weights: s and r
            aux_s = Parameter(torch.Tensor(self.n_languages, self.rank, w.data.size(0)))
            aux_r = Parameter(torch.Tensor(self.n_languages, self.rank, w.data.size(1)))

            # initialize these weights:
            nn.init.normal_(aux_s, 0.0, math.sqrt(0.02))
            nn.init.normal_(aux_r, 0.0, math.sqrt(0.02))

            setattr(self, name_w + "_s", aux_s)
            setattr(self, name_w + "_r", aux_r)
            
            if self.multiplicative:
                aux_ms = Parameter(torch.Tensor(self.n_languages, 1, w.data.size(0)))
                aux_mr = Parameter(torch.Tensor(self.n_languages, 1, w.data.size(1)))

                # initialize these weights:
                if self.activation == 'sigmoid':
                    raise NotImplementedError
                else:
                    nn.init.constant_(aux_ms, 1.0)
                    nn.init.constant_(aux_mr, 1.0)

                setattr(self, name_w + "_ms", aux_ms)
                setattr(self, name_w + "_mr", aux_mr)

    def _setweights(self, indices):
        for name_w in self.weights:
            raw_w = getattr(self.module, name_w + '_raw')
            aux_s = getattr(self, name_w + "_s")
            aux_r = getattr(self, name_w + "_r")

            s_vector = torch.index_select(aux_s, 0, indices).squeeze(0)
            r_vector = torch.index_select(aux_r, 0, indices).squeeze(0)
            w = torch.nn.functional.dropout(raw_w, p=self.dropout, training=self.training)

            if self.multiplicative:
                aux_ms = getattr(self, name_w + "_ms")
                aux_mr = getattr(self, name_w + "_mr")
                ms_vector = torch.index_select(aux_ms, 0, indices).squeeze(0)
                mr_vector = torch.index_select(aux_mr, 0, indices).squeeze(0)

                scale = torch.bmm(ms_vector.unsqueeze(-1), mr_vector.unsqueeze(1)).sum(dim=0)

                if self.activation == 'sigmoid':
                    scale = torch.sigmoid(scale)
                elif self.activation == 'tanh':
                    scale = torch.tanh(scale)

                w = w * scale

            w = w + torch.bmm(s_vector.unsqueeze(-1), r_vector.unsqueeze(1)).sum(dim=0)

            setattr(self.module, name_w, w)

    def forward(self, *args, indices=None):
        self._setweights(indices)
        return self.module.forward(*args)


if __name__ == '__main__':
    import torch
    from weight_drop_lstm import WeightDrop

    # Input is (seq, batch, input)
    x = torch.randn(2, 1, 10).cuda()
    h0 = None

    print('Testing WeightDrop')
    print('=-=-=-=-=-=-=-=-=-=')

    print('Testing WeightDrop with Linear')

    lin = WeightDrop(torch.nn.Linear(10, 10), ['weight'], dropout=0.9)
    lin.cuda()
    run1 = [x.sum() for x in lin(x).data]
    run2 = [x.sum() for x in lin(x).data]

    print('All items should be different')
    print('Run 1:', run1)
    print('Run 2:', run2)

    assert run1[0] != run2[0]
    assert run1[1] != run2[1]

    print('---')

    ###

    print('Testing WeightDrop with LSTM')

    wdrnn = WeightDrop(torch.nn.LSTM(10, 10), ['weight_hh_l0'], dropout=0.9)
    wdrnn.cuda()

    run1 = [x.sum() for x in wdrnn(x, h0)[0].data]
    run2 = [x.sum() for x in wdrnn(x, h0)[0].data]

    print('First timesteps should be equal, all others should differ')
    print('Run 1:', run1)
    print('Run 2:', run2)

    # First time step, not influenced by hidden to hidden weights, should be equal
    assert run1[0] == run2[0]
    # Second step should not
    assert run1[1] != run2[1]

    print('---')