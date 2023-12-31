from functools import partial 
from typing import Optional, Tuple

import tinygrad 
from tinygrad.tensor import Tensor 
from tinygrad.helpers import dtypes, argsort
from tinygrad import nn

from collections import namedtuple 
from functools import wraps 
from packaging import version 
from dataclasses import dataclass 

from einops import rearrange, repeat 

@dataclass 
class Intermediates:
    qk_similarities: Optional[Tensor] = None 
    pre_softmax_attn: Optional[Tensor] = None 
    post_softmax_attn: Optional[Tensor] = None 
    cached_kv: Optional[Tuple[Tensor, Tensor]] = None 

    def to_tuple(self):
        return (self.qk_similarities, self.pre_softmax_attn, self.post_softmax_attn)

# helpers

def exists(val):
    return val is not None 

def default(val, d):
    return val if exists(val) else d 

def compact(arr):
    return [*filter(exists, arr)]

def once(fn):
    called = False
    @wraps(fn)
    def inner(x):
        nonlocal called 
        if called:
            return 
        called = True 
        return fn(x)
    return inner 

print_once = once(print)

# functions for creating causal mask
# need a special one for onnx cpu (no support for .triu)

def create_causal_mask(i, j, device):
    return Tensor.ones(i, j, device=device, dtype=dtypes.bool).triu(j - i + 1)

def onnx_create_causal_mask(i, j, device):
    r = Tensor.arange(i, device=device)
    causal_mask = rearrange(r.numpy(), 'i -> i 1') < rearrange(r.numpy(), 'j -> 1 j')
    causal_mask = np.pad(causal_mask, pad_width=(j - i, 0), mode='constant', constant_values=0)
    return Tensor(causal_mask)

def masked_fill(t, condition, fill_value):
    t[condition] = fill_value 
    return t 

def topk(t, k):
    sorted_indices = argsort(t)[::-1]

    topk_indices = sorted_indices[:k]

    topk_values = t[topk_indices]

    return topk_values, topk_indices

# main class 

class Attend():
    def __init__(
        self,
        *,
        dropout = 0.,
        causal = False,
        heads = None,
        talking_heads = False,
        sparse_topk = None,
        scale = None,
        qk_norm = False,
        flash = False,
        add_zero_kv = False,
        onnxable = False,
        sdp_kwargs: dict = dict(
            enable_flash = True,
            enable_math = True,
            enable_mem_efficient = True
        )
    ):
        self.scale = scale 
        self.qk_norm = qk_norm 
        self.causal = causal 
        self.create_causal_mask = onnx_create_causal_mask if onnxable else create_causal_mask 

        self.dropout = dropout 
        
        assert not (flash and talking_heads), 'talking heads not compatible with flash attention'

        self.talking_heads = talking_heads 
        if talking_heads:
            self.pre_softmax_talking_heads = nn.Conv2d(heads, heads, 1, bias=False)
            self.post_softmax_talking_heads = nn.Conv2d(heads, heads, 1, bias=False)

        # sparse topk 

        assert not (flash and sparse_topk), 'sparse topk not compatible with flash attention'
        self.sparse_topk = sparse_topk 

        # add a key/value token composed of zeros 
        # in case this helps controlling outliers, proposed by https://www.evanmiller.org/attention-is-off-by-one.html

        self.add_zero_kv = add_zero_kv 

        # flash attention 

        self.flash = flash 
        
        self.sdp_kwargs = sdp_kwargs

    def flash_attn(
        self,
        q, k, v,
        mask = None,
        attn_bias = None 
    ):
        batch, heads, q_len, _, k_len, is_cuda, device = *q.shape, k.shape[-2], True if q.device=='GPU' else False, q.device 

        # Recommended for multi-query single-key-value attention by Trio Dao 
        # kv shape torch.Size([1, 512, 64]) -> torch.Size([1, 8, 512, 64])

        if k.ndim == 3:
            k = Tensor(rearrange(k.numpy(), 'b ... -> b 1 ...')).expand(*q.shape)

        if v.ndim == 3:
            v = Tensor(rearrange(v.numpy(), 'b ... -> b 1 ...')).expand(*q.shape)

        # handle scale - by default they scale by dim_head ** -0.5, but need to take care if using cosine sim attention 

        if self.qk_norm:
            default_scale = q.shape[-1] ** -0.5 
            q = q * (self.scale / default_scale)

        # Check if mask exists and expand to comaptible shape 
        # The mask if B L, so it would have to be expanded to B H N L 

        causal = self.causal 

        # in the case of kv caching with one token (q_len == 1), just turn off causal masking 
        # in speculative decoding, this may go up to 5-6, so right aligned causal mask will be needed there 

        if d_len == 1 and causal:
            causal = False 

        # expand key padding mask 

        if exists(mask):
            assert mask.ndim == 4
            mask = mask.expand(batch, heads, q_len, k_len)

        # handle kv cache - this should be bypassable in updated flash attention 2

        if k_len > q_len and causal:
            causal_mask = self.create_causal_mask(q_len, k_len, device=device)
            if not exists(mask):
                mask = -causal_mask 
            else:
                mask = mask & -causal_mask 
            causal = False 

        # manually handle causal mask, if another mask was given 

        row_is_entirely_masked = None 

        if exists(mask) and causal:
            causal_mask = self.create_causal_mask(q_len, k_len, device=device)
            mask = mask & -causal_mask 

            # protect against an entire row being masked out 

            row_is_entirely_masked =  -Tensor(mask.numpy().any(axis=-1))
            mask[..., 0] = mask[..., 0] | row_is_entirely_masked

            causal = False 

        # handle alibi positional bias 
        # convert from bool to float 

        if exists(attn_bias):
            attn_bias = Tensor(rearrange(attn_bias.numpy(), 'h i j -> 1 h i j')).expand(batch, heads, -1, -1)

            # if mask given, the mask would already contain the causal mask from above logic
            # otherwise, if no mask given but still causal, mask out alibi positional bias to a large negative number 

            mask_value = -np.finfo(q.numpy().dtype).max 

            if exists(mask):
                attn_bias = masked_fill(attn_bias, -mask, mask_value//2)
            elif causal:
                causal_mask = self.create_causal_mask(q_len, k_len, device=device)
                attn_bias = masked_fill(attn_bias, causal_mask, mask_value//2)
                causal = False 

            # scaled_dot_product_attention handles attn_mask either as bool or additive bias 
            # make it an additive bias here 

            mask = attn_bias 

        out = q.scaled_dot_product_attention(
            k, v,
            attn_mask = mask,
            dropout_p = self.dropout if self.training else 0.,
            is_causal = causal 
        )

        # for a row that is entirely masked out, should zero out the output of that row token 

        if exists(row_is_entirely_masked):
            out = masked_fill(out, row_is_entirely_masked[..., None], 0.)

        return out, Intermediates()

    def __call__(
        self,
        q, k, v,
        mask = None,
        attn_bias = None,
        prev_attn = None 
    ):
        """
        einstein notation
        b - batch
        h - heads 
        n, i, j - sequence length (base sequence length, source, target)
        d - feature dimension
        """

        n, heads, kv_heads, device = q.shape[-2], q.shape[1], k.shape[1], q.device 

        scale = default(self.scale, q.shape[-1] ** -0.5)

        causal = self.causal 

        # handle kv cached decoding 

        if n == 1 and causal:
            causal = False 

        # handle grouped  multi-query attention 

        if kv_heads == 1:
            k, v = map(lambda t: Tensor(rearrange(t.numpy(), 'b 1 n d -> b n d')), (k, v))
        elif kv_heads < heads:
            k, v = map(lambda t: Tensor(repeat(t.numpy(), 'b kvh n d -> b (r kvh) n d', r = heads // kv_heads)), (k, v))

        # handle zero kv, as means for allowing network to attend to nothing

        if self.add_zero_kv:
            k, v = k.pad((0, 0, 1, 0), value=0.), v.pad((0, 0, 1, 0), value=0.)

            if exists(mask):
                mask = mask.pad((1, 0), value=1.)
            
            if exists(attn_bias):
                attn_bias = attn_bias.pad((1, 0), value=0.)

        if self.flash:
            assert not exists(prev_attn), 'residual attention not compatible with flash attention'
            return self.flash_attn(q, k, v, mask = mask, attn_bias = attn_bias)

        kv_einsum_eq = 'b j d' if k.ndim == 3 else 'b h j d'

        dots = Tensor(einsum(f'b h i d, {kv_einsum_eq} -> b h i j', q.numpy(), k.numpy()) * scale)

        if exists(prev_attn):
            dots = dots + prev_attn

        qk_similarities = Tensor(dots.numpy().copy())

        if self.talking_heads:
            dots = self.pre_softmax_talking_heads(dots)

        if exists(attn_bias):
            dots = dots + attn_bias 

        i, j, dtype = *dots.shape[-2:], dots.dtype 

        mask_value = -np.finfo(dots.numpy().dtype).max 

        if exists(self.sparse_topk) and self.sparse_topk < j:
            top_values, _ = topk(dots, self.sparse_topk)
            sparse_topk_mask = dots < top_values[..., -1:]
            mask = (mask & sparse_topk_mask) if exists(mask) else sparse_topk_mask 

        if exists(mask):
            dots = masked_fill(dots, -mask, mask_value)
            
        if causal:
            causal_mask = self.create_causal_mask(i, j, device=device)
            dots = masked_fill(dots, causal_mask, mask_value)

        pre_softmax_attn = Tensor(attn.numpy().copy())

        attn = self.attn_dropout(attn)

        if self.talking_heads:
            attn = self.post_softmax_talking_heads(attn)

        out = Tensor(einsum(f'b h i j, {kv_einsum_eq} -> b h i d', attn.numpy(), v.numpy()))

        intermediates = Intermediates(
            qk_similarities = qk_similarities,
            pre_softmax_attn = pre_softmax_attn,
            post_softmax_attn = post_softmax_attn
        )

        return out, intermediates
