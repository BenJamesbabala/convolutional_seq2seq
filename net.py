# encoding: utf-8

import numpy as np

import chainer
from chainer import cuda
import chainer.functions as F
import chainer.links as L
from chainer import reporter


def sentence_block_embed(embed, x):
    batch, length = x.shape
    e = embed(x.reshape((batch * length, )))
    return e.reshape((batch, e.shape[1], length))


def seq_linear(linear, x):
    batch, units, length, _ = x.shape
    h = linear(F.transpose(x, (0, 2, 1, 3)).reshape(batch * length, units))
    return F.transpose(h.reshape((batch, length, units, 1)), (0, 2, 1, 3))


class VarInNormal(chainer.initializer.Initializer):

    """Initializes array with root-scaled Gaussian distribution.

    Each element of the array is initialized by the value drawn
    independently from Gaussian distribution whose mean is 0,
    and standard deviation is
    :math:`\\sqrt{\\frac{scale}{fan_{in}}}`,
    where :math:`fan_{in}` is the number of input units.

    Args:
        scale (float): A constant that determines the scale
            of the variance.
        dtype: Data type specifier.

    """

    def __init__(self, scale=1.0, dtype=None):
        self.scale = scale
        super(VarInNormal, self).__init__(dtype)

    def __call__(self, array):
        if self.dtype is not None:
            assert array.dtype == self.dtype
        fan_in, fan_out = chainer.initializer.get_fans(array.shape)
        s = np.sqrt(self.scale / fan_in)
        chainer.initializers.normal.Normal(s)(array)


class ConvGLU(chainer.Chain):
    def __init__(self, n_units, width=5, dropout=0.2, nopad=False):
        init_conv = VarInNormal(4. * (1. - dropout))
        super(ConvGLU, self).__init__(
            conv=L.Convolution2D(
                n_units, 2 * n_units,
                ksize=(width, 1),
                stride=(1, 1),
                pad=(width // 2 * (1 - nopad), 0),
                initialW=init_conv)
        )
        self.dropout = dropout

    def __call__(self, x):
        x = F.dropout(x, ratio=self.dropout)
        out, pregate = F.split_axis(self.conv(x), 2, axis=1)
        return out * F.sigmoid(pregate)

# TODO: For layers whose output is not directly fed to a gated linear
# unit, we initialize weights from N (0, p 1/nl) where nl is the number of
# input connections for each neuron.

# TODO: For convolutional decoders with multiple attention, we
# scale the gradients for the encoder layers by the number
# of attention mechanisms we use; we exclude source word
# embeddings


class ConvGLUEncoder(chainer.Chain):
    def __init__(self, n_layers, n_units, width=5, dropout=0.2):
        super(ConvGLUEncoder, self).__init__()
        links = [('l{}'.format(i + 1),
                  ConvGLU(n_units, width=width, dropout=dropout))
                 for i in range(n_layers)]
        for link in links:
            self.add_link(*link)
        self.conv_names = [name for name, _ in links]

    def __call__(self, x):
        scale = 0.5 ** 0.5
        for name in self.conv_names:
            x = x + getattr(self, name)(x)
            x *= scale
        return x


class ConvGLUDecoder(chainer.Chain):
    def __init__(self, n_layers, n_units, width=5, dropout=0.2):
        super(ConvGLUDecoder, self).__init__()
        links = [('l{}'.format(i + 1),
                  ConvGLU(n_units, width=(width // 2 + 1),
                          dropout=dropout, nopad=True))
                 for i in range(n_layers)]
        for link in links:
            self.add_link(*link)
        self.conv_names = [name for name, _ in links]

        init_preatt = VarInNormal(1.)
        links = [('preatt{}'.format(i + 1),
                  L.Linear(n_units, n_units, initialW=init_preatt))
                 for i in range(n_layers)]
        for link in links:
            self.add_link(*link)
        self.preatt_names = [name for name, _ in links]

    def __call__(self, x, z, ze, mask):
        scale = 0.5 ** 0.5
        att_scale = self.xp.sum(
            mask, axis=2, keepdims=True)[:, None, :, :] ** 0.5
        pad = self.xp.zeros(
            (x.shape[0], x.shape[1], 2, 1), dtype=x.dtype)
        base_x = x
        z = F.squeeze(z, axis=3)
        for conv_name, preatt_name in zip(self.conv_names, self.preatt_names):
            x = x + getattr(self, conv_name)(F.concat([pad, x], axis=2))
            x *= scale
            preatt = seq_linear(getattr(self, preatt_name), x)
            query = base_x + preatt
            query = F.squeeze(x, axis=3)
            c = self.attend(query, z, ze, mask)
            c *= att_scale
            x = x + c
        return x

    def attend(self, query, key, value, mask, minfs=None):
        # TODO reshape
        # (b, units, dec_xl) (b, units, enc_l) (b, units, dec_l, enc_l)
        pre_a = F.batch_matmul(query, key, transa=True)
        # (b, dec_xl, enc_l)
        minfs = self.xp.full(pre_a.shape, -np.inf, pre_a.dtype) \
            if minfs is None else minfs
        pre_a = F.where(mask, pre_a, minfs)
        a = F.softmax(pre_a, axis=2)
        # if values in axis=2 are all -inf, they become nan. thus do re-mask.
        a = F.where(self.xp.isnan(a.data),
                    self.xp.zeros(a.shape, dtype=a.dtype), a)
        reshaped_a = a[:, None]
        # (b, 1, dec_xl, enc_l)
        pre_c = F.broadcast_to(reshaped_a, value.shape) * value
        c = F.sum(pre_c, axis=3, keepdims=True)
        # (b, units, dec_xl, 1)
        return c


class Seq2seq(chainer.Chain):

    def __init__(self, n_layers, n_source_vocab, n_target_vocab, n_units,
                 max_length=1024, dropout=0.2):
        init_emb = chainer.initializers.Normal(0.1)
        init_out = VarInNormal(1.)
        super(Seq2seq, self).__init__(
            embed_x=L.EmbedID(n_source_vocab, n_units, ignore_label=-1,
                              initialW=init_emb),
            embed_y=L.EmbedID(n_target_vocab, n_units, ignore_label=-1,
                              initialW=init_emb),
            embed_position_x=L.EmbedID(max_length, n_units,
                                       initialW=init_emb),
            embed_position_y=L.EmbedID(max_length, n_units,
                                       initialW=init_emb),
            encoder=ConvGLUEncoder(n_layers, n_units, 5, dropout),
            decoder=ConvGLUDecoder(n_layers, n_units, 5, dropout),
            W=L.Linear(n_units, n_target_vocab, initialW=init_out),
        )
        self.n_layers = n_layers
        self.n_units = n_units
        self.n_target_vocab = n_target_vocab
        self.max_length = max_length
        self.dropout = dropout

    def __call__(self, x_block, y_in_block, y_out_block, get_prediction=False):
        batch, x_length = x_block.shape
        batch, y_length = y_in_block.shape

        # Embed
        ex_block = sentence_block_embed(self.embed_x, x_block)
        ey_block = sentence_block_embed(self.embed_y, y_in_block)
        max_len = max(x_length, y_length)
        position_block = self.xp.broadcast_to(
            self.xp.arange(max_len, dtype='i')[None, ], (batch, max_len))
        if max_len > self.max_length:
            position_block[:, self.max_length:] = self.max_length - 1
        px_block = sentence_block_embed(
            self.embed_position_x, position_block[:, :x_length])
        py_block = sentence_block_embed(
            self.embed_position_y, position_block[:, :y_length])
        ex_block += px_block
        ey_block += py_block

        # Encode and decode before output
        z_block = self.encoder(ex_block[:, :, :, None])
        ze_block = F.broadcast_to(
            F.transpose(z_block + ex_block[:, :, :, None], (0, 1, 3, 2)),
            (batch, self.n_units, y_length, x_length))
        z_mask = (x_block.data[:, None, :] >= 0) * \
            (y_in_block.data[:, :, None] >= 0)
        h_block = self.decoder(ey_block[:, :, :, None],
                               z_block, ze_block, z_mask)
        h_block = F.squeeze(h_block, axis=3)

        # It is faster to concatenate data before calculating loss
        # because only one matrix multiplication is called.
        assert(h_block.shape == (batch, self.n_units, y_length))
        concat_h_block = F.transpose(h_block, (0, 2, 1)).reshape(
            (batch * y_length, self.n_units))
        concat_h_block = F.dropout(concat_h_block, ratio=self.dropout)
        concat_pred_block = self.W(concat_h_block)
        if get_prediction:
            pred_block = concat_pred_block.reshape(
                (batch, y_length, self.n_target_vocab))
            return pred_block
        else:
            concat_y_out_block = y_out_block.reshape((batch * y_length))
            loss = F.sum(F.softmax_cross_entropy(
                concat_pred_block, concat_y_out_block, reduce='mean'))
            reporter.report({'loss': loss.data}, self)
            perp = self.xp.exp(loss.data)
            reporter.report({'perp': perp}, self)
            return loss

    def translate(self, x_block, max_length=50):
        # TODO: efficient inference by re-using convolution result
        with chainer.no_backprop_mode():
            if isinstance(x_block, list):
                x_block = chainer.dataset.convert.concat_examples(
                    x_block, device=None, padding=-1)
            batch, x_length = x_block.shape
            y_block = self.xp.zeros((batch, 1), dtype=x_block.dtype)
            x_block = chainer.Variable(x_block)
            y_block = chainer.Variable(y_block)
            eos_flags = self.xp.zeros((batch, ), dtype=x_block.dtype)

            result = []
            for i in range(max_length):
                log_prob_block = self(x_block, y_block, y_block,
                                      get_prediction=True)
                log_prob_tail = log_prob_block[:, -1, :]
                ys = self.xp.argmax(log_prob_tail.data, axis=1).astype('i')
                result.append(ys)
                y_block = F.concat([y_block, ys[:, None]], axis=1)
                eos_flags += (ys == 0)
                if self.xp.all(eos_flags):
                    break

        result = cuda.to_cpu(self.xp.stack(result).T)

        # Remove EOS taggs
        outs = []
        for y in result:
            inds = np.argwhere(y == 0)
            if len(inds) > 0:
                y = y[:inds[0, 0]]
            if len(y) == 0:
                y = np.array([1], 'i')
            outs.append(y)
        return outs
