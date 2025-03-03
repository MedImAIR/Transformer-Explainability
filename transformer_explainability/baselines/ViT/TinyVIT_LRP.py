import itertools
from typing import Tuple

import torch
from torch import nn
import torch.nn.functional as F
import torch.utils.checkpoint as checkpoint

from timm.models.layers import to_2tuple, trunc_normal_
from timm.models.registry import register_model
from timm.models.helpers import build_model_with_cfg

from einops import rearrange

from transformer_explainability.modules.layers_ours import Add, AdaptiveAvgPool1d, BatchNorm2d, Clone, Conv2d, DropPath, Dropout, \
    einsum, GELU, Identity, LayerNorm, Linear, Softmax


class Conv2d_BN(nn.Sequential):
    """
    Готово
    """
    def __init__(self, a, b, ks=1, stride=1, pad=0, dilation=1, groups=1, bn_weight_init=1):
        super().__init__()
        self.add_module('c', Conv2d(
            a, b, ks, stride, pad, dilation, groups, bias=False))
        bn = BatchNorm2d(b)
        torch.nn.init.constant_(bn.weight, bn_weight_init)
        torch.nn.init.constant_(bn.bias, 0)
        self.add_module('bn', bn)

    @torch.no_grad()
    def fuse(self):
        c, bn = self._modules.values()
        w = bn.weight / (bn.running_var + bn.eps)**0.5
        w = c.weight * w[:, None, None, None]
        b = bn.bias - bn.running_mean * bn.weight / \
            (bn.running_var + bn.eps)**0.5
        m = Conv2d(w.size(1) * self.c.groups, w.size(0), w.shape[2:], 
            stride=self.c.stride, padding=self.c.padding, dilation=self.c.dilation, groups=self.c.groups
        )
        m.weight.data.copy_(w)
        m.bias.data.copy_(b)
        return m
    
    def relprop(self, cam, **kwargs):
        cam = self._modules['bn'].relprop(cam, **kwargs)
        cam = self._modules['c'].relprop(cam, **kwargs)
        return cam


class PatchEmbed(nn.Module):
    """
    Готово
    """
    def __init__(self, in_chans, embed_dim, resolution, activation):
        super().__init__()
        img_size: Tuple[int, int] = to_2tuple(resolution)
        self.patches_resolution = (img_size[0] // 4, img_size[1] // 4)
        self.num_patches = self.patches_resolution[0] * \
            self.patches_resolution[1]
        self.in_chans = in_chans
        self.embed_dim = embed_dim
        n = embed_dim
        self.seq = nn.Sequential(
            Conv2d_BN(in_chans, n // 2, 3, 2, 1),
            activation(),
            Conv2d_BN(n // 2, n, 3, 2, 1),
        )

    def forward(self, x):
        return self.seq(x)
    
    def relprop(self, cam, **kwargs):
        for module in reversed(self.seq):
            cam = module.relprop(cam, **kwargs)
        return cam


class PatchMerging(nn.Module):
    """
    Готово
    """
    def __init__(self, input_resolution, dim, out_dim, activation):
        super().__init__()

        self.input_resolution = input_resolution
        self.dim = dim
        self.out_dim = out_dim
        self.act = activation()
        self.conv1 = Conv2d_BN(dim, out_dim, 1, 1, 0)
        self.conv2 = Conv2d_BN(out_dim, out_dim, 3, 2, 1, groups=out_dim)
        self.conv3 = Conv2d_BN(out_dim, out_dim, 1, 1, 0)

    def forward(self, x):
        self.ndim = x.ndim
        if x.ndim == 3:
            H, W = self.input_resolution
            B = len(x)
            # (B, C, H, W)
            x = x.view(B, H, W, -1).permute(0, 3, 1, 2)

        x = self.conv1(x)
        x = self.act(x)
        x = self.conv2(x)
        x = self.act(x)
        x = self.conv3(x)

        x = x.flatten(2).transpose(1, 2)
        return x
    
    def relprop(self, cam, **kwargs):
        B, HW, C = cam.shape
        H, W = self.input_resolution
        cam = cam.transpose(1, 2).reshape(B, C, H // 2, W // 2)

        cam = self.conv3.relprop(cam, **kwargs)
        cam = self.act.relprop(cam, **kwargs)
        cam = self.conv2.relprop(cam, **kwargs)
        cam = self.act.relprop(cam, **kwargs)
        cam = self.conv1.relprop(cam, **kwargs)

        if self.ndim == 3:
            B = len(cam)
            H, W = self.input_resolution
            cam = cam.permute(0, 2, 3, 1).reshape(B, H * W, -1)
        return cam
    

class MBConv(nn.Module):
    """
    Готово
    """
    def __init__(self, in_chans, out_chans, expand_ratio,
                 activation, drop_path):
        super().__init__()
        self.in_chans = in_chans
        self.hidden_chans = int(in_chans * expand_ratio)
        self.out_chans = out_chans

        self.clone = Clone()

        self.conv1 = Conv2d_BN(in_chans, self.hidden_chans, ks=1)
        self.act1 = activation()

        self.conv2 = Conv2d_BN(self.hidden_chans, self.hidden_chans,
                               ks=3, stride=1, pad=1, groups=self.hidden_chans)
        self.act2 = activation()

        self.conv3 = Conv2d_BN(
            self.hidden_chans, out_chans, ks=1, bn_weight_init=0.0)
        self.act3 = activation()

        self.drop_path = DropPath(drop_path) if drop_path > 0. else Identity()
        
        self.add = Add()

    def forward(self, x):

        x1, x2 = self.clone(x, 2)

        x1 = self.conv1(x1)
        x1 = self.act1(x1)

        x1 = self.conv2(x1)
        x1 = self.act2(x1)

        x1 = self.conv3(x1)

        x1 = self.drop_path(x1)

        x = self.add([x1, x2])
        x = self.act3(x)

        return x
    
    def relprop(self, cam, **kwargs):

        cam = self.act3.relprop(cam, **kwargs)
        cam1, cam2 = self.add.relprop(cam, **kwargs)

        cam1 = self.drop_path.relprop(cam1, **kwargs)

        cam1 = self.conv3.relprop(cam1, **kwargs)

        cam1 = self.act2.relprop(cam1, **kwargs)
        cam1 = self.conv2.relprop(cam1, **kwargs)

        cam1 = self.act1.relprop(cam1, **kwargs) 
        cam1 = self.conv1.relprop(cam1, **kwargs) 
        
        cam = self.clone.relprop((cam1, cam2), **kwargs)

        return cam


class ConvLayer(nn.Module):
    """
    Готово
    """
    def __init__(self, dim, input_resolution, depth,
                 activation,
                 drop_path=0., downsample=None, use_checkpoint=False,
                 out_dim=None,
                 conv_expand_ratio=4.,
                 ):

        super().__init__()
        self.dim = dim
        self.input_resolution = input_resolution
        self.depth = depth
        self.use_checkpoint = use_checkpoint

        # build blocks
        self.blocks = nn.ModuleList([
            MBConv(dim, dim, conv_expand_ratio, activation,
                   drop_path[i] if isinstance(drop_path, list) else drop_path,
                   )
            for i in range(depth)])

        # patch merging layer
        if downsample is not None:
            self.downsample = downsample(
                input_resolution, dim=dim, out_dim=out_dim, activation=activation)
        else:
            self.downsample = None

    def forward(self, x):
        for blk in self.blocks:
            if self.use_checkpoint:
                x = checkpoint.checkpoint(blk, x)
            else:
                x = blk(x)
        if self.downsample is not None:
            x = self.downsample(x)
        return x
    
    def relprop(self, cam, **kwargs):
        if self.downsample is not None:
            cam = self.downsample.relprop(cam, **kwargs)
        for blk in reversed(self.blocks):
            cam = blk.relprop(cam, **kwargs)
        return cam
    

class Attention(nn.Module):
    """
    Готово
    """
    def __init__(self, dim, key_dim, num_heads=8,
                 attn_ratio=4,
                 resolution=(14, 14),
                 ):
        super().__init__()
        # (h, w)
        assert isinstance(resolution, tuple) and len(resolution) == 2
        self.num_heads = num_heads
        self.scale = key_dim ** -0.5
        self.key_dim = key_dim
        self.nh_kd = nh_kd = key_dim * num_heads
        self.d = int(attn_ratio * key_dim)
        self.dh = int(attn_ratio * key_dim) * num_heads
        self.attn_ratio = attn_ratio
        h = self.dh + nh_kd * 2

        self.norm = LayerNorm(dim)
        self.qkv = Linear(dim, h)
        self.softmax = Softmax(dim=-1)
        self.proj = Linear(self.dh, dim)

        # A = Q*K^T
        self.matmul1 = einsum('bhid,bhjd->bhij')
        # attn = A*V
        self.matmul2 = einsum('bhij,bhjd->bhid')

        points = list(itertools.product(
            range(resolution[0]), range(resolution[1])))
        N = len(points)
        attention_offsets = {}
        idxs = []
        for p1 in points:
            for p2 in points:
                offset = (abs(p1[0] - p2[0]), abs(p1[1] - p2[1]))
                if offset not in attention_offsets:
                    attention_offsets[offset] = len(attention_offsets)
                idxs.append(attention_offsets[offset])
        self.attention_biases = torch.nn.Parameter(
            torch.zeros(num_heads, len(attention_offsets)))
        self.register_buffer('attention_bias_idxs',
                             torch.LongTensor(idxs).view(N, N),
                             persistent=False)

    @torch.no_grad()
    def train(self, mode=True):
        super().train(mode)
        if mode and hasattr(self, 'ab'):
            del self.ab
        else:
            self.ab = self.attention_biases[:, self.attention_bias_idxs]

    def save_v(self, v):
        self.v = v
    
    def save_attn(self, attn):
        self.attn = attn
    
    def save_v_cam(self, cam):
        self.v_cam = cam

    def save_attn_cam(self, cam):
        self.attn_cam = cam

    def get_attn_cam(self):
        return self.attn_cam

    def save_attn_gradients(self, attn_gradients):
        self.attn_gradients = attn_gradients

    def get_attn_gradients(self):
        return self.attn_gradients
    
    def forward(self, x):  # x (B,N,C)
        B, N, _ = x.shape

        # Normalization
        x = self.norm(x)

        qkv = self.qkv(x)
        # (B, num_heads, N, d)
        q, k, v = rearrange(qkv, 'b n (qkv h d) -> qkv b h n d', qkv=3, h=self.num_heads)

        self.save_v(v) 

        attn = self.matmul1([q, k]) * self.scale
        attn = attn + \
            (self.attention_biases[:, self.attention_bias_idxs]
             if self.training else self.ab)
        attn = self.softmax(attn)

        self.save_attn(attn)
        attn.register_hook(self.save_attn_gradients)

        x = self.matmul2([attn, v])
        x = rearrange(x, 'b h n d -> b n (h d)')

        x = self.proj(x)

        return x
    
    def relprop(self, cam, **kwargs):
        cam = self.proj.relprop(cam, **kwargs)

        cam = rearrange(cam, 'b n (h d) -> b h n d', h=self.num_heads)
        cam_attn, cam_v = self.matmul2.relprop(cam, **kwargs)
        cam_attn /= 2
        cam_v /= 2

        self.save_v_cam(cam_v)
        self.save_attn_cam(cam_attn)

        cam_attn = self.softmax.relprop(cam_attn, **kwargs)
        cam_q, cam_k = self.matmul1.relprop(cam_attn, **kwargs)
        cam_q /= 2
        cam_k /= 2

        cam = rearrange([cam_q, cam_k, cam_v], 'qkv b h n d -> b n (qkv h d)', qkv=3, h=self.num_heads)
        cam = self.qkv.relprop(cam, **kwargs)
        
        cam = self.norm.relprop(cam, **kwargs)

        return cam
    

class Mlp(nn.Module):
    """
    Готово
    """
    def __init__(self, in_features, hidden_features=None,
                 out_features=None, act_layer=GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.norm = LayerNorm(in_features)
        self.fc1 = Linear(in_features, hidden_features)
        self.fc2 = Linear(hidden_features, out_features)
        self.act = act_layer()
        self.drop = Dropout(drop)

    def forward(self, x):
        x = self.norm(x)

        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x
    
    def relprop(self, cam, **kwargs):
        cam = self.drop.relprop(cam, **kwargs)
        cam = self.fc2.relprop(cam, **kwargs)
        cam = self.drop.relprop(cam, **kwargs)
        cam = self.act.relprop(cam, **kwargs)
        cam = self.fc1.relprop(cam, **kwargs)
        cam = self.norm.relprop(cam, **kwargs)
        return cam
    

class TinyViTBlock(nn.Module):
    r""" TinyViT Block.

    Готово!

    Args:
        dim (int): Number of input channels.
        input_resolution (tuple[int, int]): Input resulotion.
        num_heads (int): Number of attention heads.
        window_size (int): Window size.
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
        drop (float, optional): Dropout rate. Default: 0.0
        drop_path (float, optional): Stochastic depth rate. Default: 0.0
        local_conv_size (int): the kernel size of the convolution between
                               Attention and MLP. Default: 3
        activation: the activation function. Default: nn.GELU
    """

    def __init__(self, dim, input_resolution, num_heads, window_size=7,
                 mlp_ratio=4., drop=0., drop_path=0.,
                 local_conv_size=3,
                 activation=GELU,
                 ):
        super().__init__()
        self.dim = dim
        self.input_resolution = input_resolution
        self.num_heads = num_heads
        assert window_size > 0, 'window_size must be greater than 0'
        self.window_size = window_size
        self.mlp_ratio = mlp_ratio

        self.clone1 = Clone()
        self.drop_path = DropPath(drop_path) if drop_path > 0. else Identity()

        assert dim % num_heads == 0, 'dim must be divisible by num_heads'
        head_dim = dim // num_heads

        window_resolution = (window_size, window_size)
        self.attn = Attention(dim, head_dim, num_heads,
                              attn_ratio=1, resolution=window_resolution)

        mlp_hidden_dim = int(dim * mlp_ratio)
        mlp_activation = activation
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim,
                       act_layer=mlp_activation, drop=drop)
        
        self.add1 = Add()

        pad = local_conv_size // 2
        self.local_conv = Conv2d_BN(
            dim, dim, ks=local_conv_size, stride=1, pad=pad, groups=dim)
        
        self.clone2 = Clone()
        self.add2 = Add()

    def forward(self, x):
        H, W = self.input_resolution
        B, L, C = x.shape
        assert L == H * W, "input feature has wrong size"
        res_x, x = self.clone1(x, 2)
        if H == self.window_size and W == self.window_size:
            x = self.attn(x)
        else:
            x = x.view(B, H, W, C)
            pad_b = (self.window_size - H %
                     self.window_size) % self.window_size
            pad_r = (self.window_size - W %
                     self.window_size) % self.window_size
            padding = pad_b > 0 or pad_r > 0

            if padding:
                x = F.pad(x, (0, 0, 0, pad_r, 0, pad_b))

            pH, pW = H + pad_b, W + pad_r
            nH = pH // self.window_size
            nW = pW // self.window_size
            # window partition
            x = x.view(B, nH, self.window_size, nW, self.window_size, C).transpose(2, 3).reshape(
                B * nH * nW, self.window_size * self.window_size, C
            )
            x = self.attn(x)
            # window reverse
            x = x.view(B, nH, nW, self.window_size, self.window_size,
                       C).transpose(2, 3).reshape(B, pH, pW, C)

            if padding:
                x = x[:, :H, :W].contiguous()

            x = x.view(B, L, C)

        x = self.add1([res_x, self.drop_path(x)])

        x = x.transpose(1, 2).reshape(B, C, H, W)
        # x.shape: (B, C, H, W)
        x = self.local_conv(x)
        # x.shape: (B, L, C)
        x = x.view(B, C, L).transpose(1, 2)

        x1, x2 = self.clone2(x, 2)
        x = self.add2([x1, self.drop_path(self.mlp(x2))])
        return x

    def extra_repr(self) -> str:
        return f"dim={self.dim}, input_resolution={self.input_resolution}, num_heads={self.num_heads}, " \
               f"window_size={self.window_size}, mlp_ratio={self.mlp_ratio}"
    
    def relprop(self, cam , **kwargs):
        cam1, cam2 = self.add2.relprop(cam, **kwargs)
        cam2 = self.drop_path.relprop(cam2, **kwargs)
        cam2 = self.mlp.relprop(cam2, **kwargs)
        cam = self.clone2.relprop((cam1, cam2), **kwargs)

        B, L, C = cam.shape
        H, W = self.input_resolution
        cam = cam.transpose(1, 2).reshape(B, C, H, W)
        cam = self.local_conv.relprop(cam, **kwargs)
        cam = cam.reshape(B, C, L).transpose(1, 2)

        res_cam, cam = self.add1.relprop(cam, **kwargs)
        cam = self.drop_path.relprop(cam, **kwargs)

        if H == self.window_size and W == self.window_size:
            cam = self.attn.relprop(cam, **kwargs)
        else:
            pad_b = (self.window_size - H %
                     self.window_size) % self.window_size
            pad_r = (self.window_size - W %
                     self.window_size) % self.window_size
            padding = pad_b > 0 or pad_r > 0

            pH, pW = H + pad_b, W + pad_r
            nH = pH // self.window_size
            nW = pW // self.window_size

            cam = cam.reshape(B, H, W, C)

            if padding:
                cam = F.pad(cam, (0, 0, 0, pad_r, 0, pad_b))
            
            cam = cam.view(B, nH, self.window_size, nW, self.window_size, C).transpose(2, 3).reshape(
                B * nH * nW, self.window_size * self.window_size, C
            )
            cam = self.attn.relprop(cam, **kwargs)
            cam = cam.view(B, nH, nW, self.window_size, self.window_size,
                       C).transpose(2, 3).reshape(B, pH, pW, C)
            
            cam = cam[:, :H, :W].view(B, L, C)

        cam = self.clone1.relprop((res_cam, cam), **kwargs)
        
        return cam

            
class BasicLayer(nn.Module):
    """ 
    Готово!
    
    A basic TinyViT layer for one stage.

    Args:
        dim (int): Number of input channels.
        input_resolution (tuple[int]): Input resolution.
        depth (int): Number of blocks.
        num_heads (int): Number of attention heads.
        window_size (int): Local window size.
        mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
        drop (float, optional): Dropout rate. Default: 0.0
        drop_path (float | tuple[float], optional): Stochastic depth rate. Default: 0.0
        downsample (nn.Module | None, optional): Downsample layer at the end of the layer. Default: None
        use_checkpoint (bool): Whether to use checkpointing to save memory. Default: False.
        local_conv_size: the kernel size of the depthwise convolution between attention and MLP. Default: 3
        activation: the activation function. Default: nn.GELU
        out_dim: the output dimension of the layer. Default: dim
    """

    def __init__(self, dim, input_resolution, depth, num_heads, window_size,
                 mlp_ratio=4., drop=0.,
                 drop_path=0., downsample=None, use_checkpoint=False,
                 local_conv_size=3,
                 activation=nn.GELU,
                 out_dim=None,
                 ):

        super().__init__()
        self.dim = dim
        self.input_resolution = input_resolution
        self.depth = depth
        self.use_checkpoint = use_checkpoint

        # build blocks
        self.blocks = nn.ModuleList([
            TinyViTBlock(dim=dim, input_resolution=input_resolution,
                         num_heads=num_heads, window_size=window_size,
                         mlp_ratio=mlp_ratio,
                         drop=drop,
                         drop_path=drop_path[i] if isinstance(
                             drop_path, list) else drop_path,
                         local_conv_size=local_conv_size,
                         activation=activation,
                         )
            for i in range(depth)])

        # patch merging layer
        if downsample is not None:
            self.downsample = downsample(
                input_resolution, dim=dim, out_dim=out_dim, activation=activation)
        else:
            self.downsample = None

    def forward(self, x):
        for blk in self.blocks:
            if self.use_checkpoint:
                x = checkpoint.checkpoint(blk, x)
            else:
                x = blk(x)
        if self.downsample is not None:
            x = self.downsample(x)
        return x

    def extra_repr(self) -> str:
        return f"dim={self.dim}, input_resolution={self.input_resolution}, depth={self.depth}"
    
    def relprop(self, cam , **kwargs):
        if self.downsample is not None:
            cam = self.downsample.relprop(cam , **kwargs)
        for blk in self.blocks:
            cam = blk.relprop(cam, **kwargs)
        return cam


class TinyViT(nn.Module):
    def __init__(self, img_size=224, in_chans=3, num_classes=1000,
                 embed_dims=[96, 192, 384, 768], depths=[2, 2, 6, 2],
                 num_heads=[3, 6, 12, 24],
                 window_sizes=[7, 7, 14, 7],
                 mlp_ratio=4.,
                 drop_rate=0.,
                 drop_path_rate=0.1,
                 use_checkpoint=False,
                 mbconv_expand_ratio=4.0,
                 local_conv_size=3,
                 layer_lr_decay=1.0,
                 drop_head=False,
                 ):
        super().__init__()

        self.num_classes = num_classes
        self.depths = depths
        self.num_layers = len(depths)
        self.mlp_ratio = mlp_ratio
        self.drop_head = drop_head

        activation = GELU

        self.patch_embed = PatchEmbed(in_chans=in_chans,
                                      embed_dim=embed_dims[0],
                                      resolution=img_size,
                                      activation=activation)

        patches_resolution = self.patch_embed.patches_resolution
        self.patches_resolution = patches_resolution

        # stochastic depth
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate,
                                                sum(depths))]  # stochastic depth decay rule

        # build layers
        self.layers = nn.ModuleList()
        for i_layer in range(self.num_layers):
            kwargs = dict(dim=embed_dims[i_layer],
                          input_resolution=(patches_resolution[0] // (2 ** i_layer),
                                            patches_resolution[1] // (2 ** i_layer)),
                          depth=depths[i_layer],
                          drop_path=dpr[sum(depths[:i_layer]):sum(depths[:i_layer + 1])],
                          downsample=PatchMerging if (
                              i_layer < self.num_layers - 1) else None,
                          use_checkpoint=use_checkpoint,
                          out_dim=embed_dims[min(
                              i_layer + 1, len(embed_dims) - 1)],
                          activation=activation,
                          )
            if i_layer == 0:
                layer = ConvLayer(
                    conv_expand_ratio=mbconv_expand_ratio,
                    **kwargs,
                )
            else:
                layer = BasicLayer(
                    num_heads=num_heads[i_layer],
                    window_size=window_sizes[i_layer],
                    mlp_ratio=self.mlp_ratio,
                    drop=drop_rate,
                    local_conv_size=local_conv_size,
                    **kwargs)
            self.layers.append(layer)

        # Classifier head
        self.norm_head = LayerNorm(embed_dims[-1])
        self.head = Linear(
            embed_dims[-1], num_classes) if num_classes > 0 else Identity()

        # init weights
        self.apply(self._init_weights)
        self.set_layer_lr_decay(layer_lr_decay)

        self.avgpool = AdaptiveAvgPool1d(1)

    def set_layer_lr_decay(self, layer_lr_decay):
        decay_rate = layer_lr_decay

        # layers -> blocks (depth)
        depth = sum(self.depths)
        lr_scales = [decay_rate ** (depth - i - 1) for i in range(depth)]
        print("LR SCALES:", lr_scales)

        def _set_lr_scale(m, scale):
            for p in m.parameters():
                p.lr_scale = scale

        self.patch_embed.apply(lambda x: _set_lr_scale(x, lr_scales[0]))
        i = 0
        for layer in self.layers:
            for block in layer.blocks:
                block.apply(lambda x: _set_lr_scale(x, lr_scales[i]))
                i += 1
            if layer.downsample is not None:
                layer.downsample.apply(
                    lambda x: _set_lr_scale(x, lr_scales[i - 1]))
        assert i == depth
        for m in [self.norm_head, self.head]:
            m.apply(lambda x: _set_lr_scale(x, lr_scales[-1]))

        for k, p in self.named_parameters():
            p.param_name = k

        def _check_lr_scale(m):
            for p in m.parameters():
                assert hasattr(p, 'lr_scale'), p.param_name

        self.apply(_check_lr_scale)

    def _init_weights(self, m):
        if isinstance(m, Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    @torch.jit.ignore
    def no_weight_decay_keywords(self):
        return {'attention_biases'}

    def forward_features(self, x):
        # x: (N, C, H, W)
        x = self.patch_embed(x)

        x = self.layers[0](x)
        start_i = 1

        for i in range(start_i, len(self.layers)):
            layer = self.layers[i]
            x = layer(x)

        if not self.drop_head:
            x = self.avgpool(x.transpose(1, 2)).squeeze(2)
            # x = x.mean(1)

        return x

    def forward(self, x):
        x = self.forward_features(x)
        if not self.drop_head:
            x = self.norm_head(x)
            x = self.head(x)
        return x

    def relprop(self, cam=None, method="transformer_attribution", is_ablation=False, start_layer=0, **kwargs):
        if not self.drop_head:
            cam = self.head.relprop(cam, **kwargs)
            cam = self.norm_head.relprop(cam, **kwargs)

        if not self.drop_head:
            cam = self.avgpool.relprop(cam.unsqueeze(2), **kwargs).transpose(1, 2)

        for i, layer in enumerate(reversed(self.layers[1:])):
            cam = layer.relprop(cam, **kwargs)

        if method == "full":
            cam = self.layers[0].relprop(cam, **kwargs)
            cam = self.patch_embed.relprop(cam, **kwargs)
            # sum on channels
            cam = cam.sum(dim=1)
            return cam
        
        elif method == "rollout":
            # cam rollout
            attn_cams = []
            for layer in self.layers[1:]:
                for blk in layer.blocks:
                    attn_heads = blk.attn.get_attn_cam().clamp(min=0)
                    avg_heads = (attn_heads.sum(dim=1) / attn_heads.shape[1]).detach()
                    attn_cams.append(avg_heads)
            cam = compute_rollout_attention(attn_cams, start_layer=start_layer)
            cam = cam[:, 0, 1:]
            return cam
        
        # our method, method name grad is legacy
        elif method == "transformer_attribution" or method == "grad":
            cams = []
            for layer in self.layers[1:]:
                for blk in layer.blocks:
                    grad = blk.attn.get_attn_gradients()
                    cam = blk.attn.get_attn_cam()
                    cam = cam[0].reshape(-1, cam.shape[-1], cam.shape[-1])
                    grad = grad[0].reshape(-1, grad.shape[-1], grad.shape[-1])
                    cam = grad * cam
                    cam = cam.clamp(min=0).mean(dim=0)
                    cams.append(cam.unsqueeze(0))
            rollout = compute_rollout_attention(cams, start_layer=start_layer)
            cam = rollout[:, 0, 1:]
            return cam

        return cam
    
def compute_rollout_attention(all_layer_matrices, start_layer=0):
    # adding residual consideration
    num_tokens = all_layer_matrices[0].shape[1]
    batch_size = all_layer_matrices[0].shape[0]
    eye = torch.eye(num_tokens).expand(batch_size, num_tokens, num_tokens).to(all_layer_matrices[0].device)
    all_layer_matrices = [all_layer_matrices[i] + eye for i in range(len(all_layer_matrices))]
    # all_layer_matrices = [all_layer_matrices[i] / all_layer_matrices[i].sum(dim=-1, keepdim=True)
    #                       for i in range(len(all_layer_matrices))]
    joint_attention = all_layer_matrices[start_layer]
    for i in range(start_layer+1, len(all_layer_matrices)):
        joint_attention = all_layer_matrices[i].bmm(joint_attention)
    return joint_attention
    
_checkpoint_url_format = \
    'https://github.com/wkcn/TinyViT-model-zoo/releases/download/checkpoints/{}.pth'


def _create_tiny_vit(variant, pretrained=False, **kwargs):
    # pretrained_type: 22kto1k_distill, 1k, 22k_distill
    pretrained_type = kwargs.pop('pretrained_type', '22kto1k_distill')
    assert pretrained_type in ['22kto1k_distill', '1k', '22k_distill'], \
        'pretrained_type should be one of 22kto1k_distill, 1k, 22k_distill'

    img_size = kwargs.get('img_size', 224)
    if img_size != 224:
        pretrained_type = pretrained_type.replace('_', f'_{img_size}_')

    num_classes_pretrained = 21841 if \
        pretrained_type  == '22k_distill' else 1000

    variant_without_img_size = '_'.join(variant.split('_')[:-1])
    cfg = dict(
        url=_checkpoint_url_format.format(
            f'{variant_without_img_size}_{pretrained_type}'),
        num_classes=num_classes_pretrained,
        classifier='head',
    )

    def _pretrained_filter_fn(state_dict):
        state_dict = state_dict['model']
        # filter out attention_bias_idxs
        state_dict = {k: v for k, v in state_dict.items() if \
            not k.endswith('attention_bias_idxs')}
        return state_dict

    return build_model_with_cfg(
        TinyViT, variant, pretrained,
        pretrained_cfg=cfg,
        pretrained_filter_fn=_pretrained_filter_fn,
        **kwargs)
    

@register_model
def tiny_vit_21m_224(pretrained=False, **kwargs):
    model_kwargs = dict(
        embed_dims=[96, 192, 384, 576],
        depths=[2, 2, 6, 2],
        num_heads=[3, 6, 12, 18],
        window_sizes=[7, 7, 14, 7],
        drop_path_rate=0.2,
    )
    model_kwargs.update(kwargs)
    return _create_tiny_vit('tiny_vit_21m_224', pretrained, **model_kwargs)
