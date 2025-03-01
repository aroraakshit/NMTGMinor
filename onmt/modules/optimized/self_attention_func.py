"""
Self-attention with multi-head attention.
Code is taken from apex self-attention implementation
https://github.com/NVIDIA/apex/tree/master/apex/contrib/csrc/multihead_attn
"""

import torch
import torch.nn.functional as F
from onmt.constants import double_precision


try:
    from torch.cuda.amp import custom_fwd, custom_bwd
except (ModuleNotFoundError, ImportError) as e:
    from .compat import custom_fwd, custom_bwd

try:
    import mask_softmax_dropout_cuda
except (ModuleNotFoundError, ImportError) as e:
    mask_softmax_dropout_cuda = None


def rotate_half(x):
    x1, x2 = x[..., :x.shape[-1] // 2], x[..., x.shape[-1] // 2:]
    return torch.cat((-x2, x1), dim=x1.ndim - 1) # dim=-1 triggers a bug in torch < 1.8.0


def apply_rotary_pos_emb(q, k, cos, sin):
    return (q * cos) + (rotate_half(q) * sin), (k * cos) + (rotate_half(k) * sin)


def rotate_backward(dx):
    dx2, dx1 = dx[..., :dx.shape[-1] // 2], dx[..., dx.shape[-1] // 2:]
    return torch.cat((dx1, -dx2), dim=dx1.ndim-1)


class SelfAttnFunc(torch.autograd.Function):
    @staticmethod
    @custom_fwd(cast_inputs=torch.float16)
    def forward(ctx, use_time_mask, is_training, heads, inputs,
                input_weights, output_weights,
                input_biases, output_biases,
                mask, dropout_prob,
                rotary_pos_enc, pos_emb,
                incremental, incremental_cache,
                return_coverage):
        heads_t = torch.tensor([heads])
        dropout_prob_t = torch.tensor([dropout_prob])
        null_tensor = torch.tensor([])
        head_dim = inputs.size(2) // heads
        scale_t = torch.tensor([head_dim ** -0.5])

        ctx.rotary_pos_enc = rotary_pos_enc
        ctx.return_coverage = return_coverage

        bsz, len_q = inputs.size(1), inputs.size(0)

        # Input Linear GEMM
        # input1: (activations) [seql_q, seqs, embed_dim(1024)]
        # input2: (weights)     [embed_dim*3 (3072), embed_dim (1024)] (transpose [0,1])
        # output:               [seql_q, seqs, embed_dim*3]
        # GEMM: ( (seql_q*seqs) x embed_dim ) x ( embed_dim x embed_dim*3 ) = (seql_q*seqs x embed_dim*3)
        input_lin_results = torch.addmm(input_biases,
                                        inputs.view(inputs.size(0) * inputs.size(1), inputs.size(2)),
                                        input_weights.transpose(0, 1),
                                        beta=1., alpha=1.)

        input_lin_results = input_lin_results.view(inputs.size(0), inputs.size(1), input_weights.size(0))

        # Slice out q,k,v from one big Input Linear outuput (should only impact meta data, no copies!)
        # Sequences and heads are combined to make the batch of the Batched GEMM
        # input_lin_results: [seql_q, seqs, heads(16), 3, head_dim(64)]
        # input_lin_results: [seql_q, batches=seqs*heads, 3, head_dim]
        input_lin_results = input_lin_results.view(inputs.size(0), inputs.size(1) * heads, 3, head_dim)
        queries = input_lin_results[:, :, 0, :]
        keys = input_lin_results[:, :, 1, :]
        values = input_lin_results[:, :, 2, :]

        if incremental:
            keys = keys.contiguous().view(len_q, bsz, heads * head_dim)
            values = values.contiguous().view(len_q, bsz, heads * head_dim)
            if 'k' in incremental_cache and 'v' in incremental_cache:
                keys = torch.cat([incremental_cache['k'], keys], dim=0)  # time first
                incremental_cache['k'] = keys
                values = torch.cat([incremental_cache['v'], values], dim=0)  # time first
                incremental_cache['v'] = values
            else:
                incremental_cache['k'] = keys
                incremental_cache['v'] = values
            keys = keys.view(-1, bsz * heads, head_dim)
            values = values.view(-1, bsz * heads, head_dim)

        len_k = keys.size(0)

        # apply rotary position encodings
        if rotary_pos_enc:
            assert pos_emb is not None and pos_emb is not None
            cos, sin = pos_emb
            queries_, keys_ = apply_rotary_pos_emb(queries, keys, cos, sin)
            queries.copy_(queries_)
            keys.copy_(keys_)
        else:
            sin, cos = null_tensor, null_tensor

        # Matmul1 Batched GEMMs
        # The output tensor is specified prior to the Batch GEMM because baddbmm requires its specification
        # baddbmm is used to apply the scale parameter via the Batched GEMM's alpha parameter instead of
        # a separate elementwise operation.
        # Input1: (Queries) [seql_q, seqs*heads, head_dim] tranpose(0,1)
        # Input2: (Keys)    [seql_k, seqs*heads, head_dim] transpose(0,1)
        # output:           [seqs*heads, seql_q, seql_k]
        # GEMM: Per batch: ( seql_q x head_dim ) x ( head_dim x seql_k ) = ( seql_q x seql_k )
        if queries.is_cuda:
            matmul1_results = torch.empty((queries.size(1), queries.size(0), keys.size(0)), dtype=queries.dtype,
                                          device=queries.device)
            matmul1_results = torch.baddbmm(matmul1_results, queries.transpose(0, 1),
                                            keys.transpose(0, 1).transpose(1, 2),
                                            out=matmul1_results, beta=0.0, alpha=scale_t[0])
        else:
            matmul1_results = torch.matmul(queries.transpose(0, 1), keys.transpose(0, 1).transpose(1, 2))
            matmul1_results.mul_(scale_t[0])

        if mask is not None:
            # Self Attention Time Mask
            if use_time_mask:
                assert (len(mask.size()) == 2), "Timing mask is not 2D!"
                # assert (mask.size(0) == mask.size(1)), "Sequence length should match!"
                mask = mask.to(torch.bool)
                matmul1_results = matmul1_results.masked_fill_(mask, float('-inf'))
            # Key Padding Mask
            else:
                batches, seql_q, seql_k = matmul1_results.size()
                seqs = int(batches / heads)
                matmul1_results = matmul1_results.view(seqs, heads, seql_q, seql_k)
                mask = mask.to(torch.bool)
                matmul1_results = matmul1_results.masked_fill_(mask.unsqueeze(1).unsqueeze(2), float('-inf'))
                matmul1_results = matmul1_results.view(seqs * heads, seql_q, seql_k)

        # Softmax and Dropout attention
        if mask_softmax_dropout_cuda and len_k <= 2048 \
                and matmul1_results.type() == 'torch.cuda.HalfTensor':
            dropout_mask, softmax_results, dropout_results = mask_softmax_dropout_cuda.forward(is_training, heads,
                                                                                               matmul1_results,
                                                                                               dropout_prob_t[0])
            if not is_training:
                dropout_results = softmax_results  # because the cuda returns empty craps

            ctx.fused_softmax_dropout = True
        else:
            if matmul1_results.type() == 'torch.cuda.HalfTensor':
                softmax_results = F.softmax(matmul1_results, dim=-1, dtype=torch.float32).type_as(matmul1_results)
            else:
                softmax_results = F.softmax(matmul1_results, dim=-1)

            # Dropout - is not executed for inference
            if is_training:
                dropout_results, dropout_mask = torch._fused_dropout(softmax_results, p=(1. - dropout_prob_t[0]))
            else:
                dropout_results = softmax_results
                dropout_mask = null_tensor

        # Matmul2 Batched GEMMs
        # The output tensor specification is needed here to specify the non-standard output.
        # Given that pytorch cannot currently perform autograd with an output tensor specified,
        # this requires a backward pass specified.
        # Input1: from_softmax [seqs*heads, seql_q, seql_k]
        # Input2: (values)     [seql_v, seqs*heads, head_dim] transpose(0,1)
        # Output:              [seql_q, seqs*heads, head_dim] transpose(0,1)
        # GEMM: Per batch: ( seql_q x seql_k ) x ( seql_k x head_dim ) = (seql_q x head_dim)
        if queries.is_cuda:
            matmul2_results = torch.empty((dropout_results.size(1), dropout_results.size(0), values.size(2)),
                                          dtype=dropout_results.dtype, device=queries.device).transpose(1, 0)
            matmul2_results = torch.bmm(dropout_results, values.transpose(0, 1), out=matmul2_results)
        else:
            matmul2_results = torch.matmul(dropout_results, values.transpose(0, 1))
        matmul2_results = matmul2_results.transpose(0, 1).contiguous().view(inputs.size(0), inputs.size(1),
                                                                            inputs.size(2))

        # Output Linear GEMM
        # Input1: (activations) [seql_q, seqs, embed_dim=heads*head_dim]
        # Input2: (weights)     [ embed_dim, embed_dim ] transpose(0,1)
        # Output:               [ seql_q, seqs, embed_dim ]
        # GEMM: ( seql_q*seqs x embed_dim ) x ( embed_dim x embed_dim ) = ( seql_q*seqs x embed_dim )
        outputs = torch.addmm(output_biases,
                              matmul2_results.view(inputs.size(0) * inputs.size(1), inputs.size(2)),
                              output_weights.transpose(0, 1),
                              beta=1., alpha=1.)

        outputs = outputs.view(inputs.size(0), inputs.size(1), output_weights.size(0))

        ctx.save_for_backward(heads_t,
                              scale_t,
                              matmul2_results,
                              dropout_results,
                              softmax_results,
                              input_lin_results,
                              inputs,
                              input_weights,
                              output_weights,
                              dropout_mask,
                              dropout_prob_t,
                              sin, cos)

        if return_coverage:
            return (outputs, dropout_results)
        else:
            return (outputs,)

    @staticmethod
    @custom_bwd
    def backward(ctx, output_grads, softmax_grads):
        heads_t, \
            scale_t, \
            matmul2_results, \
            dropout_results, \
            softmax_results, \
            input_lin_results, \
            inputs, \
            input_weights, \
            output_weights, \
            dropout_mask, \
            dropout_prob_t, \
            sin, cos \
                = ctx.saved_tensors

        head_dim = inputs.size(2) // heads_t[0]

        # Slice out q,k,v from one big Input Linear outuput (should only impact meta data, no copies!)
        # Sequences and heads are combined to make the batch of the Batched GEMM
        # input_lin_results: [seql_q, seqs, heads(16), 3, head_dim(64)]
        # input_lin_results: [seql_q, batches=seqs*heads, 3, head_dim]
        input_lin_results = input_lin_results.view(inputs.size(0), inputs.size(1) * heads_t[0], 3, head_dim)
        queries = input_lin_results[:, :, 0, :]
        keys = input_lin_results[:, :, 1, :]
        values = input_lin_results[:, :, 2, :]

        len_key = keys.size(0)

        # Slice out q,k,v from one big set of gradients entering the input linear's bprop
        # (should only impact meta data, no copies!)
        # The gradients are identical in size to the Input Linear outputs.
        # The tensor is declared before hand to properly slice out query, key, and value grads.
        input_lin_results_grads = torch.empty_like(input_lin_results)
        queries_grads = input_lin_results_grads[:, :, 0, :]
        keys_grads = input_lin_results_grads[:, :, 1, :]
        values_grads = input_lin_results_grads[:, :, 2, :]

        # Output Linear GEMM - DGRAD
        # Input1: (data grads)  [seql_q, seqs, embed_dim=heads*head_dim]
        # Input2: (weights)     [ embed_dim, embed_dim ]
        # Output:               [ seql_q, seqs, embed_dim ]
        # GEMM: ( seql_q*seqs x embed_dim ) x ( embed_dim x embed_dim ) = ( seql_q*seqs x embed_dim )
        output_lin_grads = torch.mm(
            output_grads.view(output_grads.size(0) * output_grads.size(1), output_grads.size(2)), output_weights)
        output_lin_grads = output_lin_grads.view(output_grads.size(0), output_grads.size(1), output_weights.size(1))
        # Output Linear GEMM - WGRAD
        # Input1: (data grads)  [seql_q*seqs, embed_dim=heads*head_dim] transpose(0,1)
        # Input2: (activations) [seql_q*seqs, embed_dim ]
        # Output:               [ seql_q, seqs, embed_dim ]
        # GEMM: ( embed_dim x seql_q*seqs ) x ( seql_q*seqs x embed_dim ) = ( embed_dim x embed_dim )
        output_weight_grads = torch.mm(
            output_grads.view(output_grads.size(0) * output_grads.size(1), output_grads.size(2)).transpose(0, 1),
            matmul2_results.view(matmul2_results.size(0) * matmul2_results.size(1), matmul2_results.size(2)))
        output_lin_grads = output_lin_grads.view(inputs.size(0), inputs.size(1) * heads_t[0], head_dim).transpose(0, 1)

        output_bias_grads = torch.sum(
            output_grads.view(output_grads.size(0) * output_grads.size(1), output_grads.size(2)), 0)

        # Matmul2 - DGRAD1
        # Input1: (data grads)  [seql_q, seqs*heads, head_dim] transpose(0,1)
        # Input2: (activations) [seql_k, seqs*heads, head_dim] transpose(0,1).transpose(1,2)
        # Output:               [seqs*heads, seql_q, seql_k]
        # GEMM: Per batch: ( seql_q x head_dim ) x ( head_dim x seql_k ) = ( seql_q x seql_k )
        matmul2_dgrad1 = torch.bmm(output_lin_grads, values.transpose(0, 1).transpose(1, 2))
        # Matmul2 - DGRAD2
        # Input1: (data grads)  [seql_q, seqs*heads, head_dim] transpose(0,1)
        # Input2: (activations) [seql_k, seqs*heads, head_dim] transpose(0,1).transpose(1,2)
        # Output:               [seqs*heads, seql_q, seql_k]
        # GEMM: Per batch: ( seql_q x head_dim ) x ( head_dim x seql_k ) = ( seql_q x seql_k )
        values_grads = torch.bmm(dropout_results.transpose(1, 2), output_lin_grads, out=values_grads.transpose(0, 1))

        if mask_softmax_dropout_cuda is not None and matmul2_dgrad1.type() == 'torch.cuda.HalfTensor' \
                and len_key <= 2048:
            softmax_grads = mask_softmax_dropout_cuda.backward_recompute(heads_t[0], matmul2_dgrad1, softmax_results,
                                                                         dropout_mask, dropout_prob_t[0])
        else:

            # Mask and Scaling for Dropout (not a publically documented op)
            dropout_grads = torch._masked_scale(matmul2_dgrad1, dropout_mask, 1.0 / (1.0 - dropout_prob_t[0]))

            # Softmax Grad (not a publically documented op)
            softmax_grads = torch._softmax_backward_data(dropout_grads, softmax_results, -1, softmax_results)

        # Matmul1 - DGRAD1
        # Input1: (data grads)  [seqs*heads, seql_q, seql_k]
        # Input2: (activations) [seql_k, seqs*heads, head_dim] transpose(0,1)
        # Output:               [seqs*heads, seql_q, head_dim] transpose(0,1)
        # GEMM: Per batch: ( seql_q x seql_k ) x ( seql_k x head_dim ) = ( seql_q x head_dim )
        torch.baddbmm(queries_grads.transpose(0, 1), softmax_grads, keys.transpose(0, 1),
                                      out=queries_grads.transpose(0, 1), beta=0.0, alpha=scale_t[0])
        # Matmul1 - DGRAD2
        # Input1: (data grads)  [seqs*heads, seql_q, seql_k] transpose(1,2)
        # Input2: (activations) [seql_q, seqs*heads, head_dim] transpose(0,1)
        # Output:               [seqs*heads, seql_k, head_dim] transpose(0,1)
        # GEMM: Per batch: ( seql_k x seql_q ) x ( seql_q x head_dim ) = ( seql_k x head_dim )
        torch.baddbmm(keys_grads.transpose(0, 1), softmax_grads.transpose(1, 2), queries.transpose(0, 1),
                                   out=keys_grads.transpose(0, 1), beta=0.0, alpha=scale_t[0])

        if ctx.rotary_pos_enc:
            queries_grads_ = queries_grads * cos + rotate_backward(sin * queries_grads)
            keys_grads_ = keys_grads * cos + rotate_backward(sin * keys_grads)
            queries_grads.copy_(queries_grads_)
            keys_grads.copy_(keys_grads_)

        # Input Linear GEMM - DGRAD
        # input1: (data grads) [seql_q, seqs, 3*embed_dim(3072)]
        # input2: (weights)    [embed_dim*3 (3072), embed_dim (1024)]
        # output:              [seql_q, seqs, embed_dim]
        # GEMM: ( (seql_q*seqs) x 3*embed_dim ) x ( 3*embed_dim x embed_dim ) = (seql_q*seqs x embed_dim)
        input_lin_results_grads = input_lin_results_grads.view(inputs.size(0) * inputs.size(1),
                                                               heads_t[0] * 3 * head_dim)
        input_grads = torch.mm(input_lin_results_grads, input_weights)
        input_grads = input_grads.view(inputs.size(0), inputs.size(1), inputs.size(2))
        # Input Linear GEMM - WGRAD
        # input1: (data grads)  [seql_q*seqs, 3*embed_dim(3072)]
        # input2: (activations) [seql_q*seqs, embed_dim(1024)]
        # output:               [3*embed_dim, embed_dim]
        # GEMM: ( 3*embed_dim x seql_q*seqs ) x ( seql_q*seqs x embed_dim ) = (3*embed_dim x embed_dim)
        input_weight_grads = torch.mm(input_lin_results_grads.transpose(0, 1),
                                      inputs.view(inputs.size(0) * inputs.size(1), inputs.size(2)))

        input_bias_grads = torch.sum(input_lin_results_grads, 0)

        return None, None, None, \
            input_grads, \
            input_weight_grads, output_weight_grads, \
            input_bias_grads, output_bias_grads, \
            None, None, None, None, None, None, None


self_attn_func = SelfAttnFunc.apply
