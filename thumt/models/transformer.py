# coding=utf-8
# Copyright 2017-2019 The THUMT Authors

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import torch
import torch.nn as nn

import thumt.utils as utils
from thumt.modules import MultiHeadAttention, FeedForward, PositionalEmbedding


class AttentionSubLayer(nn.Module):

    def __init__(self, params):
        super(AttentionSubLayer, self).__init__()
        self.attention = MultiHeadAttention(params.hidden_size,
                                            params.num_heads,
                                            params.attention_dropout)
        self.layer_norm = nn.LayerNorm(params.hidden_size)
        self.dropout = nn.Dropout(params.residual_dropout)

    def forward(self, x, bias, memory=None, state=None):
        if self.training or state is None:
            y = self.attention(x, bias, memory, None)
        else:
            kv = [state["k"], state["v"]]
            y, k, v = self.attention(x, bias, memory, kv)
            state["k"], state["v"] = k, v

        return self.layer_norm(x + self.dropout(y))


class FFNSubLayer(nn.Module):

    def __init__(self, params, dtype=None):
        super(FFNSubLayer, self).__init__()
        self.ffn_layer = FeedForward(params.hidden_size, params.filter_size,
                                     dropout=params.relu_dropout)
        self.layer_norm = nn.LayerNorm(params.hidden_size)
        self.dropout = nn.Dropout(params.residual_dropout)

    def forward(self, x):
        y = self.ffn_layer(x)

        return self.layer_norm(x + self.dropout(y))


class TransformerEncoderLayer(nn.Module):

    def __init__(self, params):
        super(TransformerEncoderLayer, self).__init__()
        self.self_attention = AttentionSubLayer(params)
        self.feed_forward = FFNSubLayer(params)

    def forward(self, x, bias):
        x = self.self_attention(x, bias)
        x = self.feed_forward(x)
        return x


class TransformerDecoderLayer(nn.Module):

    def __init__(self, params):
        super(TransformerDecoderLayer, self).__init__()
        self.self_attention = AttentionSubLayer(params)
        self.encdec_attention = AttentionSubLayer(params)
        self.feed_forward = FFNSubLayer(params)

    def __call__(self, x, attn_bias, encdec_bias, memory, state=None):
        x = self.self_attention(x, attn_bias, state=state)
        x = self.encdec_attention(x, encdec_bias, memory)
        x = self.feed_forward(x)
        return x


class TransformerEncoder(nn.Module):

    def __init__(self, params):
        super(TransformerEncoder, self).__init__()
        self.layers = nn.ModuleList([
            TransformerEncoderLayer(params)
            for i in range(params.num_encoder_layers)])

    def forward(self, x, bias):
        for layer in self.layers:
            x = layer(x, bias)
        return x


class TransformerDecoder(nn.Module):

    def __init__(self, params):
        super(TransformerDecoder, self).__init__()
        self.layers = nn.ModuleList([
            TransformerDecoderLayer(params)
            for i in range(params.num_encoder_layers)])

    def forward(self, x, attn_bias, encdec_bias, memory, state=None):
        for i, layer in enumerate(self.layers):
            if state is not None:
                x = layer(x, attn_bias, encdec_bias, memory,
                          state["decoder"]["layer_%d" % i])
            else:
                x = layer(x, attn_bias, encdec_bias, memory, None)
        return x


class Transformer(nn.Module):

    def __init__(self, params):
        super(Transformer, self).__init__()
        self.params = params
        self.build_embedding(params)
        self.encoding = PositionalEmbedding()
        self.dropout = nn.Dropout(params.residual_dropout)
        self.encoder = TransformerEncoder(params)
        self.decoder = TransformerDecoder(params)
        self.hidden_size = params.hidden_size
        self.num_encoder_layers = params.num_encoder_layers
        self.num_decoder_layers = params.num_decoder_layers
        self.reset_parameters()

    def build_embedding(self, params):
        src_vocab_size = len(params.vocabulary["source"])
        tgt_vocab_size = len(params.vocabulary["target"])

        self.source_embedding = torch.nn.Parameter(
            torch.empty([src_vocab_size, params.hidden_size]))

        if params.shared_source_target_embedding:
            self.target_embedding = self.source_embedding
        else:
            self.target_embedding = torch.nn.Parameter(
                torch.empty([tgt_vocab_size, params.hidden_size]))

        if params.shared_embedding_and_softmax_weights:
            self.softmax_weights = self.target_embedding
        else:
            self.softmax_weights = torch.nn.Parameter(
                torch.empty([tgt_vocab_size, params.hidden_size]))

        self.bias = torch.nn.Parameter(torch.zeros([params.hidden_size]))

    def reset_parameters(self):
        nn.init.normal_(self.source_embedding, mean=0,
                        std=self.params.hidden_size ** -0.5)
        nn.init.normal_(self.target_embedding, mean=0,
                        std=self.params.hidden_size ** -0.5)

    def encode(self, features, state):
        src_seq = features["source"]
        src_mask = torch.ne(src_seq, 0).float()
        enc_attn_bias = self.masking_bias(src_mask)

        inputs = torch.nn.functional.embedding(src_seq, self.source_embedding)
        inputs = inputs * (self.hidden_size ** 0.5)
        inputs = inputs + self.bias
        inputs = self.dropout(self.encoding(inputs))

        enc_attn_bias = enc_attn_bias.to(inputs)
        encoder_output = self.encoder(inputs, enc_attn_bias)

        state["encoder_output"] = encoder_output
        state["enc_attn_bias"] = enc_attn_bias

        return state

    def decode(self, features, state, mode="infer"):
        tgt_seq = features["target"]

        enc_attn_bias = state["enc_attn_bias"]
        dec_attn_bias = self.causal_bias(tgt_seq.shape[1])

        targets = torch.nn.functional.embedding(tgt_seq, self.target_embedding)
        targets = targets * (self.hidden_size ** 0.5)

        decoder_input = torch.cat(
            [targets.new_zeros([targets.shape[0], 1, targets.shape[-1]]),
             targets[:, 1:, :]], dim=1)
        decoder_input = self.dropout(self.encoding(decoder_input))

        encoder_output = state["encoder_output"]
        dec_attn_bias = dec_attn_bias.to(targets)

        if mode == "infer":
            decoder_input = decoder_input[:, -1:, :]
            dec_attn_bias = dec_attn_bias[:, :, -1:, :]

        decoder_output = self.decoder(decoder_input, dec_attn_bias,
                                      enc_attn_bias, encoder_output, state)

        decoder_output = torch.reshape(decoder_output, [-1, self.hidden_size])
        decoder_output = torch.transpose(decoder_output, -1, -2)
        logits = torch.matmul(self.softmax_weights, decoder_output)
        logits = torch.transpose(logits, 0, 1)

        return logits, state

    def forward(self, features):
        labels = torch.reshape(features["labels"], [-1, 1])
        state = self.empty_state(features["target"].shape[0],
                                 labels.device)
        state = self.encode(features, state)
        logits, _ = self.decode(features, state, "train")

        return logits

    def empty_state(self, batch_size, device):
        state = {
            "decoder": {
                "layer_%d" % i: {
                    "k": torch.zeros([batch_size, 0, self.hidden_size],
                                     device=device),
                    "v": torch.zeros([batch_size, 0, self.hidden_size],
                                     device=device)
                } for i in range(self.num_decoder_layers)
            }
        }

        return state

    @staticmethod
    def masking_bias(mask, inf=-1e9):
        ret = (1.0 - mask) * inf
        return torch.unsqueeze(torch.unsqueeze(ret, 1), 1)

    @staticmethod
    def causal_bias(length, inf=-1e9):
        ret = torch.ones([length, length]) * inf
        ret = torch.triu(ret, diagonal=1)
        return torch.reshape(ret, [1, 1, length, length])

    @staticmethod
    def base_params():
        params = utils.HParams(
            pad="<pad>",
            bos="<eos>",
            eos="<eos>",
            unk="<unk>",
            hidden_size=512,
            filter_size=2048,
            num_heads=8,
            num_encoder_layers=6,
            num_decoder_layers=6,
            attention_dropout=0.0,
            residual_dropout=0.1,
            relu_dropout=0.0,
            label_smoothing=0.1,
            shared_embedding_and_softmax_weights=False,
            shared_source_target_embedding=False,
            # Override default parameters
            train_steps=100000,
            learning_rate=7e-4,
            learning_rate_schedule="linear_warmup_rsqrt_decay",
            batch_size=4096,
            fixed_batch_size=False,
            adam_beta1=0.9,
            adam_beta2=0.98,
            adam_epsilon=1e-9,
            clip_grad_norm=0.0
        )

        return params

    @staticmethod
    def big_params():
        params = Transformer.base_params()
        params.hidden_size = 1024
        params.filter_size = 4096
        params.num_heads = 16
        params.residual_dropout = 0.3
        params.learning_rate = 5e-4
        params.train_steps = 300000

        return params

    @staticmethod
    def default_params(name=None):
        if name == "base":
            return Transformer.base_params()
        elif name == "big":
            return Transformer.big_params()
        else:
            return Transformer.base_params()
