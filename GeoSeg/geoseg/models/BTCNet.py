""" BTCNet
Paper: ``
    - https://arxiv.org/abs/2104.01136

ResT code and weights: https://github.com/wofmanaf/ResT
"""
import torch
import torch.nn as nn
from torch.nn import Module, Conv2d, Parameter, Softmax
from torchvision import models
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
from geoseg.models.MIRNet import *
from geoseg.models.swin import SwinTransformer


import torch.nn.functional as F
import numpy as np


def to_one_hot(inp, num_classes):
    y_onehot = torch.FloatTensor(inp.size(0), num_classes).to(inp.device)
    y_onehot.zero_()
    y_onehot.scatter_(1, inp.unsqueeze(1).data, 1)
    return y_onehot


bce_loss = nn.BCELoss()
softmax = nn.Softmax(dim=1)


class ManifoldMixupModel(nn.Module):
    def __init__(self, model, num_classes=6, alpha=1):
        super().__init__()
        self.model = model
        self.alpha = alpha
        self.lam = None
        self.num_classes = num_classes
        ##选择需要操作的层，在ResNet中各block的层名为layer1,layer2...所以可以写成如下。其他网络请自行修改
        self.module_list = []
        for n, m in self.model.named_modules():
            # if 'conv' in n:
            if n[:-1] == 'layer':
                self.module_list.append(m)

    def forward(self, x, target=None):
        if target == None:
            out = self.model(x)
            return out
        else:
            if self.alpha <= 0:
                self.lam = 1
            else:
                self.lam = np.random.beta(self.alpha, self.alpha)
            k = np.random.randint(-1, len(self.module_list))
            self.indices = torch.randperm(target.size(0)).cuda()
            target_onehot = to_one_hot(target, self.num_classes)
            target_shuffled_onehot = target_onehot[self.indices]
            if k == -1:
                x = x * self.lam + x[self.indices] * (1 - self.lam)
                out = self.model(x)
            else:
                modifier_hook = self.module_list[k].register_forward_hook(self.hook_modify)
                out = self.model(x)
                modifier_hook.remove()
            target_reweighted = target_onehot * self.lam + target_shuffled_onehot * (1 - self.lam)

            loss = bce_loss(softmax(out), target_reweighted)
            return out, loss

    def hook_modify(self, module, input, output):
        output = self.lam * output + (1 - self.lam) * output[self.indices]
        return output


class PatchUpModel(nn.Module):
    def __init__(self, model, num_classes=6, block_size=7, gamma=.9, patchup_type='hard', keep_prob=.9):
        super().__init__()
        self.patchup_type = patchup_type
        self.block_size = block_size
        self.gamma = gamma
        self.gamma_adj = None
        self.kernel_size = (block_size, block_size)
        self.stride = (1, 1)
        self.padding = (block_size // 2, block_size // 2)
        self.computed_lam = None

        self.model = model
        self.num_classes = num_classes
        self.module_list = []
        for n, m in self.model.named_modules():
            if n[:-1] == 'layer':
                # if 'conv' in n:
                self.module_list.append(m)

    def adjust_gamma(self, x):
        return self.gamma * x.shape[-1] ** 2 / \
               (self.block_size ** 2 * (x.shape[-1] - self.block_size + 1) ** 2)

    def forward(self, x, target=None):
        if target == None:
            out = self.model(x)
            return out
        else:

            self.lam = np.random.beta(2.0, 2.0)
            k = np.random.randint(-1, len(self.module_list))
            self.indices = torch.randperm(target.size(0)).cuda()
            self.target_onehot = to_one_hot(target, self.num_classes)
            self.target_shuffled_onehot = self.target_onehot[self.indices]

            if k == -1:  # CutMix
                W, H = x.size(2), x.size(3)
                cut_rat = np.sqrt(1. - self.lam)
                cut_w = np.int(W * cut_rat)
                cut_h = np.int(H * cut_rat)
                cx = np.random.randint(W)
                cy = np.random.randint(H)

                bbx1 = np.clip(cx - cut_w // 2, 0, W)
                bby1 = np.clip(cy - cut_h // 2, 0, H)
                bbx2 = np.clip(cx + cut_w // 2, 0, W)
                bby2 = np.clip(cy + cut_h // 2, 0, H)

                x[:, :, bbx1:bbx2, bby1:bby2] = x[self.indices, :, bbx1:bbx2, bby1:bby2]
                lam = 1 - ((bbx2 - bbx1) * (bby2 - bby1) / (W * H))
                out = self.model(x)
                loss = bce_loss(softmax(out), self.target_onehot) * lam + \
                       bce_loss(softmax(out), self.target_shuffled_onehot) * (1. - lam)

            else:
                modifier_hook = self.module_list[k].register_forward_hook(self.hook_modify)
                out = self.model(x)
                modifier_hook.remove()

                loss = 1.0 * bce_loss(softmax(out), self.target_a) * self.total_unchanged_portion + \
                       bce_loss(softmax(out), self.target_b) * (1. - self.total_unchanged_portion) + \
                       1.0 * bce_loss(softmax(out), self.target_reweighted)
            return out, loss

    def hook_modify(self, module, input, output):
        self.gamma_adj = self.adjust_gamma(output)
        p = torch.ones_like(output[0]) * self.gamma_adj
        m_i_j = torch.bernoulli(p)
        mask_shape = len(m_i_j.shape)
        m_i_j = m_i_j.expand(output.size(0), m_i_j.size(0), m_i_j.size(1), m_i_j.size(2))
        holes = F.max_pool2d(m_i_j, self.kernel_size, self.stride, self.padding)
        mask = 1 - holes
        unchanged = mask * output
        if mask_shape == 1:
            total_feats = output.size(1)
        else:
            total_feats = output.size(1) * (output.size(2) ** 2)
        total_changed_pixels = holes[0].sum()
        total_changed_portion = total_changed_pixels / total_feats
        self.total_unchanged_portion = (total_feats - total_changed_pixels) / total_feats
        if self.patchup_type == 'hard':
            self.target_reweighted = self.total_unchanged_portion * self.target_onehot + \
                                     total_changed_portion * self.target_shuffled_onehot
            patches = holes * output[self.indices]
            self.target_b = self.target_onehot[self.indices]
        elif self.patchup_type == 'soft':
            self.target_reweighted = self.total_unchanged_portion * self.target_onehot + \
                                     self.lam * total_changed_portion * self.target_onehot + \
                                     (1 - self.lam) * total_changed_portion * self.target_shuffled_onehot
            patches = holes * output
            patches = patches * self.lam + patches[self.indices] * (1 - self.lam)
            self.target_b = self.lam * self.target_onehot + (1 - self.lam) * self.target_shuffled_onehot
        else:
            raise ValueError("patchup_type must be \'hard\' or \'soft\'.")

        output = unchanged + patches
        self.target_a = self.target_onehot
        return output


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class Attention(nn.Module):
    def __init__(self,
                 dim,
                 num_heads=8,
                 qkv_bias=False,
                 qk_scale=None,
                 attn_drop=0.,
                 proj_drop=0.,
                 sr_ratio=1,
                 apply_transform=False):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.kv = nn.Linear(dim, dim * 2, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        self.sr_ratio = sr_ratio
        if sr_ratio > 1:
            self.sr = nn.Conv2d(dim, dim, kernel_size=sr_ratio+1, stride=sr_ratio, padding=sr_ratio // 2, groups=dim)
            self.sr_norm = nn.LayerNorm(dim)

        self.apply_transform = apply_transform and num_heads > 1
        if self.apply_transform:
            self.transform_conv = nn.Conv2d(self.num_heads, self.num_heads, kernel_size=1, stride=1)
            self.transform_norm = nn.InstanceNorm2d(self.num_heads)

    def forward(self, x, H, W):
        B, N, C = x.shape
        q = self.q(x).reshape(B, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
        if self.sr_ratio > 1:
            x_ = x.permute(0, 2, 1).reshape(B, C, H, W)
            x_ = self.sr(x_).reshape(B, C, -1).permute(0, 2, 1)
            x_ = self.sr_norm(x_)
            kv = self.kv(x_).reshape(B, -1, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        else:
            kv = self.kv(x).reshape(B, N, 2, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        k, v = kv[0], kv[1]

        attn = (q @ k.transpose(-2, -1)) * self.scale
        if self.apply_transform:
            attn = self.transform_conv(attn)
            attn = attn.softmax(dim=-1)
            attn = self.transform_norm(attn)
        else:
            attn = attn.softmax(dim=-1)

        attn = self.attn_drop(attn)
        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class Block(nn.Module):
    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, sr_ratio=1, apply_transform=False):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, sr_ratio=sr_ratio, apply_transform=apply_transform)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

    def forward(self, x, H, W):
        x = x + self.drop_path(self.attn(self.norm1(x), H, W))
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x


class PA(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.pa_conv = nn.Conv2d(dim, dim, kernel_size=3, padding=1, groups=dim)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        return x * self.sigmoid(self.pa_conv(x))


class GL(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.gl_conv = nn.Conv2d(dim, dim, kernel_size=3, padding=1, groups=dim)

    def forward(self, x):
        return x + self.gl_conv(x)


class PatchEmbed(nn.Module):
    """ Image to Patch Embedding"""
    def __init__(self, patch_size=16, in_ch=3, out_ch=768, with_pos=False):
        super().__init__()
        self.patch_size = to_2tuple(patch_size)
        self.conv = nn.Conv2d(in_ch, out_ch, kernel_size=patch_size+1, stride=patch_size, padding=patch_size // 2)
        self.norm = nn.BatchNorm2d(out_ch)

        self.with_pos = with_pos
        if self.with_pos:
            self.pos = PA(out_ch)

    def forward(self, x):
        B, C, H, W = x.shape
        x = self.conv(x)
        x = self.norm(x)
        if self.with_pos:
            x = self.pos(x)
        x = x.flatten(2).transpose(1, 2)
        H, W = H // self.patch_size[0], W // self.patch_size[1]
        return x, (H, W)


class BasicStem(nn.Module):
    def __init__(self, in_ch=3, out_ch=64, with_pos=False):
        super(BasicStem, self).__init__()
        hidden_ch = out_ch // 2
        self.conv1 = nn.Conv2d(in_ch, hidden_ch, kernel_size=3, stride=2, padding=1, bias=False)
        self.norm1 = nn.BatchNorm2d(hidden_ch)
        self.conv2 = nn.Conv2d(hidden_ch, hidden_ch, kernel_size=3, stride=1, padding=1, bias=False)
        self.norm2 = nn.BatchNorm2d(hidden_ch)
        self.conv3 = nn.Conv2d(hidden_ch, out_ch, kernel_size=3, stride=2, padding=1, bias=False)

        self.act = nn.ReLU(inplace=True)
        self.with_pos = with_pos
        if self.with_pos:
            self.pos = PA(out_ch)

    def forward(self, x):
        x = self.conv1(x)
        x = self.norm1(x)
        x = self.act(x)

        x = self.conv2(x)
        x = self.norm2(x)
        x = self.act(x)

        x = self.conv3(x)
        if self.with_pos:
            x = self.pos(x)
        return x


class ResT(nn.Module):
    def __init__(self, in_chans=3, num_classes=1000, embed_dims=[64, 128, 256, 512],
                 num_heads=[1, 2, 4, 8], mlp_ratios=[4, 4, 4, 4], qkv_bias=False,
                 qk_scale=None, drop_rate=0., attn_drop_rate=0., drop_path_rate=0.,
                 depths=[2, 2, 2, 2], sr_ratios=[8, 4, 2, 1],
                 norm_layer=nn.LayerNorm, apply_transform=False):
        super().__init__()
        self.num_classes = num_classes
        self.depths = depths
        self.apply_transform = apply_transform

        self.stem = BasicStem(in_ch=in_chans, out_ch=embed_dims[0], with_pos=True)

        self.patch_embed_2 = PatchEmbed(patch_size=2, in_ch=embed_dims[0], out_ch=embed_dims[1], with_pos=True)
        self.patch_embed_3 = PatchEmbed(patch_size=2, in_ch=embed_dims[1], out_ch=embed_dims[2], with_pos=True)
        self.patch_embed_4 = PatchEmbed(patch_size=2, in_ch=embed_dims[2], out_ch=embed_dims[3], with_pos=True)

        # transformer encoder
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]
        cur = 0

        self.stage1 = nn.ModuleList([
            Block(embed_dims[0], num_heads[0], mlp_ratios[0], qkv_bias, qk_scale, drop_rate, attn_drop_rate,
                  drop_path=dpr[cur+i], norm_layer=norm_layer, sr_ratio=sr_ratios[0], apply_transform=apply_transform)
            for i in range(self.depths[0])])

        cur += depths[0]
        self.stage2 = nn.ModuleList([
            Block(embed_dims[1], num_heads[1], mlp_ratios[1], qkv_bias, qk_scale, drop_rate, attn_drop_rate,
                  drop_path=dpr[cur+i], norm_layer=norm_layer, sr_ratio=sr_ratios[1], apply_transform=apply_transform)
            for i in range(self.depths[1])])

        cur += depths[1]
        self.stage3 = nn.ModuleList([
            Block(embed_dims[2], num_heads[2], mlp_ratios[2], qkv_bias, qk_scale, drop_rate, attn_drop_rate,
                  drop_path=dpr[cur+i], norm_layer=norm_layer, sr_ratio=sr_ratios[2], apply_transform=apply_transform)
            for i in range(self.depths[2])])

        cur += depths[2]
        self.stage4 = nn.ModuleList([
            Block(embed_dims[3], num_heads[3], mlp_ratios[3], qkv_bias, qk_scale, drop_rate, attn_drop_rate,
                  drop_path=dpr[cur+i], norm_layer=norm_layer, sr_ratio=sr_ratios[3], apply_transform=apply_transform)
            for i in range(self.depths[3])])

        self.norm = norm_layer(embed_dims[3])

        # init weights
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Conv2d):
            trunc_normal_(m.weight, std=0.02)
        elif isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=0.02)
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, (nn.LayerNorm, nn.BatchNorm2d)):
            nn.init.constant_(m.weight, 1.0)
            nn.init.constant_(m.bias, 0)

    def forward(self, x):
        x = self.stem(x)
        B, _, H, W = x.shape
        x = x.flatten(2).permute(0, 2, 1)

        # stage 1
        for blk in self.stage1:
            x = blk(x, H, W)
        x = x.permute(0, 2, 1).reshape(B, -1, H, W)
        # x1 = x

        # stage 2
        x, (H, W) = self.patch_embed_2(x)
        for blk in self.stage2:
            x = blk(x, H, W)
        x = x.permute(0, 2, 1).reshape(B, -1, H, W)
        # x2 = x

        # stage 3
        x, (H, W) = self.patch_embed_3(x)
        for blk in self.stage3:
            x = blk(x, H, W)
        x = x.permute(0, 2, 1).reshape(B, -1, H, W)
        x3 = x

        # stage 4
        x, (H, W) = self.patch_embed_4(x)
        for blk in self.stage4:
            x = blk(x, H, W)
        x = self.norm(x)
        x = x.permute(0, 2, 1).reshape(B, -1, H, W)
        x4 = x

        return x3, x4


def rest_lite(pretrained=True, weight_path='pretrain_weights/rest_lite.pth',  **kwargs):
    model = ResT(embed_dims=[64, 128, 256, 512], num_heads=[1, 2, 4, 8], mlp_ratios=[4, 4, 4, 4], qkv_bias=True,
                 depths=[2, 2, 2, 2], sr_ratios=[8, 4, 2, 1], apply_transform=True, **kwargs)
    if pretrained and weight_path is not None:
        old_dict = torch.load(weight_path)
        model_dict = model.state_dict()
        old_dict = {k: v for k, v in old_dict.items() if (k in model_dict)}
        model_dict.update(old_dict)
        model.load_state_dict(model_dict)
    return model


class ConvBNReLU(nn.Module):
    def __init__(self, in_chan, out_chan, ks=3, stride=1, padding=1):
        super(ConvBNReLU, self).__init__()
        self.conv = nn.Conv2d(in_chan,
                              out_chan,
                              kernel_size=ks,
                              stride=stride,
                              padding=padding,
                              bias=False)
        self.bn = nn.BatchNorm2d(out_chan)
        self.relu = nn.ReLU(inplace=True)
        self.init_weight()

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.relu(x)
        return x

    def init_weight(self):
        for ly in self.children():
            if isinstance(ly, nn.Conv2d):
                nn.init.kaiming_normal_(ly.weight, a=1)
                if not ly.bias is None: nn.init.constant_(ly.bias, 0)


def l2_norm(x):
    return torch.einsum("bcn, bn->bcn", x, 1 / torch.norm(x, p=2, dim=-2))


class LinearAttention(Module):
    def __init__(self, in_places, scale=8, eps=1e-6):
        super(LinearAttention, self).__init__()
        self.gamma = Parameter(torch.zeros(1))
        self.in_places = in_places
        self.l2_norm = l2_norm
        self.eps = eps

        self.query_conv = Conv2d(in_channels=in_places, out_channels=in_places // scale, kernel_size=1)
        self.key_conv = Conv2d(in_channels=in_places, out_channels=in_places // scale, kernel_size=1)
        self.value_conv = Conv2d(in_channels=in_places, out_channels=in_places, kernel_size=1)

    def forward(self, x):
        # Apply the feature map to the queries and keys
        batch_size, chnnels, height, width = x.shape
        Q = self.query_conv(x).view(batch_size, -1, width * height)
        K = self.key_conv(x).view(batch_size, -1, width * height)
        V = self.value_conv(x).view(batch_size, -1, width * height)

        Q = self.l2_norm(Q).permute(-3, -1, -2)
        K = self.l2_norm(K)

        tailor_sum = 1 / (width * height + torch.einsum("bnc, bc->bn", Q, torch.sum(K, dim=-1) + self.eps))
        value_sum = torch.einsum("bcn->bc", V).unsqueeze(-1)
        value_sum = value_sum.expand(-1, chnnels, width * height)

        matrix = torch.einsum('bmn, bcn->bmc', K, V)
        matrix_sum = value_sum + torch.einsum("bnm, bmc->bcn", Q, matrix)

        weight_value = torch.einsum("bcn, bn->bcn", matrix_sum, tailor_sum)
        weight_value = weight_value.view(batch_size, chnnels, height, width)

        return x + (self.gamma * weight_value).contiguous()


class Output(nn.Module):
    def __init__(self, in_chan, mid_chan, n_classes, up_factor=32, *args, **kwargs):
        super(Output, self).__init__()
        self.up_factor = up_factor
        out_chan = n_classes * up_factor * up_factor
        self.conv = ConvBNReLU(in_chan, mid_chan, ks=3, stride=1, padding=1)
        self.conv_out = nn.Conv2d(mid_chan, out_chan, kernel_size=1, bias=True)
        self.up = nn.PixelShuffle(up_factor)
        self.init_weight()

    def forward(self, x):
        x = self.conv(x)
        x = self.conv_out(x)
        x = self.up(x)
        return x

    def init_weight(self):
        for ly in self.children():
            if isinstance(ly, nn.Conv2d):
                nn.init.kaiming_normal_(ly.weight, a=1)
                if not ly.bias is None: nn.init.constant_(ly.bias, 0)

    def get_params(self):
        wd_params, nowd_params = [], []
        for name, module in self.named_modules():
            if isinstance(module, (nn.Linear, nn.Conv2d)):
                wd_params.append(module.weight)
                if not module.bias is None:
                    nowd_params.append(module.bias)
            elif isinstance(module, nn.modules.batchnorm._BatchNorm):
                nowd_params += list(module.parameters())
        return wd_params, nowd_params


class UpSample(nn.Module):

    def __init__(self, n_chan, factor=2):
        super(UpSample, self).__init__()
        out_chan = n_chan * factor * factor
        self.proj = nn.Conv2d(n_chan, out_chan, 1, 1, 0)
        self.up = nn.PixelShuffle(factor)
        self.init_weight()

    def forward(self, x):
        feat = self.proj(x)
        feat = self.up(feat)
        return feat

    def init_weight(self):
        nn.init.xavier_normal_(self.proj.weight, gain=1.)


class Attention_Embedding(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(Attention_Embedding, self).__init__()
        self.attention = LinearAttention(in_channels)
        self.conv_attn = ConvBNReLU(in_channels, out_channels)
        self.up = UpSample(out_channels)

    def forward(self, high_feat, low_feat):
        A = self.attention(high_feat)
        A = self.conv_attn(A)
        A = self.up(A)

        output = low_feat * A
        output += low_feat

        return output

class PPM(nn.Module):
    def __init__(self, in_dim, reduction_dim, bins):
        super(PPM, self).__init__()
        self.features = []
        for bin in bins:
            self.features.append(nn.Sequential(
                nn.AdaptiveAvgPool2d(bin),
                nn.Conv2d(in_dim, reduction_dim, kernel_size=1, bias=False),
                #nn.BatchNorm2d(reduction_dim),
                nn.ReLU(inplace=True)
            ))
        self.features = nn.ModuleList(self.features)


    def forward(self, x):
        x_size = x.size()
        out = [x]
        for f in self.features:
            out.append(F.interpolate(f(x), x_size[2:], mode='bilinear', align_corners=True))   #what is f(x)
        return torch.cat(out, 1)

# class FeatureAggregationModule(nn.Module):
#     def __init__(self, in_chan, out_chan):
#         super(FeatureAggregationModule, self).__init__()
#         self.convblk = ConvBNReLU(in_chan, out_chan, ks=1, stride=1, padding=0)
#         self.conv_atten = LinearAttention(out_chan)
#         self.init_weight()
#
#     def forward(self, fsp, fcp):
#         fcat = torch.cat([fsp, fcp], dim=1)
#         feat = self.convblk(fcat)
#         atten = self.conv_atten(feat)
#         feat_atten = torch.mul(feat, atten)
#         feat_out = feat_atten + feat
#         return feat_out
#
#     def init_weight(self):
#         for ly in self.children():
#             if isinstance(ly, nn.Conv2d):
#                 nn.init.kaiming_normal_(ly.weight, a=1)
#                 if not ly.bias is None: nn.init.constant_(ly.bias, 0)
#
#     def get_params(self):
#         wd_params, nowd_params = [], []
#         for name, module in self.named_modules():
#             if isinstance(module, (nn.Linear, nn.Conv2d)):
#                 wd_params.append(module.weight)
#                 if not module.bias is None:
#                     nowd_params.append(module.bias)
#             elif isinstance(module, nn.modules.batchnorm._BatchNorm):
#                 nowd_params += list(module.parameters())
#         return wd_params, nowd_params
class FeatureAggregationModule(nn.Module):
    def __init__(self, in_channels=3, out_channels=3, bias=False):
        """Initializes U-Net."""

        super(FeatureAggregationModule, self).__init__()
        self.embedding_dim = 3
        # self.conv0 = nn.Conv2d(3, self.embedding_dim, 3, stride=1, padding=1)
        self.conv1 = nn.Conv2d(256 * 3, 256, 3, stride=1, padding=1)
        self.conv1_1 = nn.Conv2d(384, 256, 3, stride=1, padding=1)
        self.conv1_2 = nn.Conv2d(384, 256, 3, stride=1, padding=1)
        self.conv2 = nn.Conv2d(128 * 3, 128, 3, stride=1, padding=1)
        self.conv2_1 = nn.Conv2d(192, 128, 3, stride=1, padding=1)
        self.conv2_2 = nn.Conv2d(192, 128, 3, stride=1, padding=1)
        self.conv3 = nn.Conv2d(64 * 3, 64, 3, stride=1, padding=1)
        self.conv3_1 = nn.Conv2d(96, 64, 3, stride=1, padding=1)
        self.conv3_2 = nn.Conv2d(96, 64, 3, stride=1, padding=1)
        self.conv3_3 = nn.Conv2d(48, 24, 3, stride=1, padding=1)
        self.conv4 = nn.Conv2d(32, 32, 3, stride=1, padding=1)
        self.conv4_1 = nn.Conv2d(96, 48, 3, stride=1, padding=1)
        self.conv4_2 = nn.Conv2d(48, 24, 3, stride=1, padding=1)
        self.conv4_3 = nn.Conv2d(24, 12, 3, stride=1, padding=1)
        self.conv4_4 = nn.Conv2d(12, 12, 3, stride=1, padding=1)
        self.in_chans = 3
        self.maxpool = nn.MaxPool2d(2, stride=2, return_indices=False, ceil_mode=False)
        self.upsample = nn.UpsamplingBilinear2d(scale_factor=2)
        self.ReLU = nn.ReLU(inplace=True)
        self.IN_1 = nn.InstanceNorm2d(48, affine=False)
        self.IN_2 = nn.InstanceNorm2d(96, affine=False)
        self.IN_3 = nn.InstanceNorm2d(192, affine=False)
        self.PPM1 = PPM(32, 8, bins=(1, 2, 3, 4))
        self.PPM2 = PPM(64, 16, bins=(1, 2, 3, 4))
        self.PPM3 = PPM(128, 32, bins=(1, 2, 3, 4))
        self.PPM4 = PPM(256, 64, bins=(1, 2, 3, 4))

        self.MSRB1 = MSRB(256, 3, 1, 2, bias)
        self.MSRB2 = MSRB(128, 3, 1, 2, bias)
        self.MSRB3 = MSRB(64, 3, 1, 2, bias)
        self.MSRB4 = MSRB(32, 3, 1, 2, bias)

        # 27,565,242
        self.swin_1 = SwinTransformer(pretrain_img_size=224,
                                      patch_size=2,
                                      in_chans=3,
                                      embed_dim=96,
                                      depths=[2, 2, 2],
                                      num_heads=[3, 6, 12],
                                      window_size=7,
                                      mlp_ratio=4.,
                                      qkv_bias=True,
                                      qk_scale=None,
                                      drop_rate=0.,
                                      attn_drop_rate=0.,
                                      drop_path_rate=0.2,
                                      norm_layer=nn.LayerNorm,
                                      ape=False,
                                      patch_norm=True,
                                      out_indices=(0, 1, 2),
                                      frozen_stages=-1,
                                      use_checkpoint=False)

        self.E_block1 = nn.Sequential(
            nn.Conv2d(3, 32, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 32, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2, stride=2, return_indices=False, ceil_mode=False))

        self.E_block2 = nn.Sequential(
            nn.Conv2d(64, 64, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(64, 64, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2, stride=2, return_indices=False, ceil_mode=False))

        self.E_block3 = nn.Sequential(
            nn.Conv2d(128, 128, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 128, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2, stride=2, return_indices=False, ceil_mode=False))

        self.E_block4 = nn.Sequential(
            nn.Conv2d(256, 256, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(256, 256, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2, stride=2, return_indices=False, ceil_mode=False))

        self.E_block5 = nn.Sequential(
            nn.Conv2d(512, 512, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 512, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2, stride=2, return_indices=False, ceil_mode=False))

        self._block1 = nn.Sequential(
            nn.Conv2d(512, 256, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(256, 256, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.UpsamplingBilinear2d(scale_factor=2))

        self._block2 = nn.Sequential(
            nn.Conv2d(512, 256, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(256, 256, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.UpsamplingBilinear2d(scale_factor=2))

        self._block3 = nn.Sequential(
            nn.Conv2d(256, 128, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, 128, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.UpsamplingBilinear2d(scale_factor=2))

        self._block4 = nn.Sequential(
            nn.Conv2d(128, 64, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(64, 64, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.UpsamplingBilinear2d(scale_factor=2))

        self._block5 = nn.Sequential(
            nn.Conv2d(64, 32, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            # nn.UpsamplingBilinear2d(scale_factor=2),
            nn.Conv2d(32, 32, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.UpsamplingBilinear2d(scale_factor=2))

        self._block6 = nn.Sequential(
            nn.Conv2d(46, 23, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            # nn.UpsamplingBilinear2d(scale_factor=2),
            nn.Conv2d(23, 23, 3, stride=1, padding=1),
            nn.UpsamplingBilinear2d(scale_factor=2))

        self._block7 = nn.Sequential(
            nn.Conv2d(32, 32, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            # nn.UpsamplingBilinear2d(scale_factor=2),
            nn.Conv2d(32, out_channels, 3, stride=1, padding=1))

        # Initialize weights
        # self._init_weights()

    def _init_weights(self):
        """Initializes weights using He et al. (2015)."""

        for m in self.modules():
            if isinstance(m, nn.ConvTranspose2d) or isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight.data)
                m.bias.data.zero_()

    def forward(self, fcp, fsp):
        """Through encoder, then decoder by adding U-skip connections. """
        swin_in = fcp  # 96,192,384,768
        swin_out_1 = self.swin_1(swin_in)
        # swin_out = self.swin(swin_in)
        # Encoder
        swin_input_1 = self.E_block1(swin_in)  # 32
        swin_input_1 = self.PPM1(swin_input_1)

        swin_input_2 = self.E_block2(swin_input_1)  # 64
        swin_input_2 = self.PPM2(swin_input_2)

        swin_input_3 = self.E_block3(swin_input_2)  # 128
        swin_input_3 = self.PPM3(swin_input_3)

        swin_input_4 = self.E_block4(swin_input_3)  # 256
        swin_input_4 = self.PPM4(swin_input_4)
        # swin_input_5=self.E_block5(swin_input_4)#512
        # import pdb
        # pdb.set_trace()

        # transformer
        upsample1 = self._block1(swin_input_4)  # 256
        # upsample1=[self.MSRB1(upsample1)]
        # import pdb
        # pdb.set_trace()
        beta_1 = self.conv1_1(swin_out_1[2])
        gamma_1 = self.conv1_2(swin_out_1[2])
        swin_input_3_refine = self.IN_3(swin_input_3) * beta_1 + gamma_1  # 128
        concat3 = torch.cat((swin_input_3, swin_input_3_refine, upsample1), dim=1)  # 256+256+256==512
        decoder_3 = self.ReLU(self.conv1(concat3))  # 256
        upsample3 = self._block3(decoder_3)  # 128
        upsample3 = self.MSRB2(upsample3)

        beta_2 = self.conv2_1(swin_out_1[1])
        gamma_2 = self.conv2_2(swin_out_1[1])
        swin_input_2_refine = self.IN_2(swin_input_2) * beta_2 + gamma_2  # 64
        concat2 = torch.cat((swin_input_2, swin_input_2_refine, upsample3), dim=1)  # 128+128+128=256
        decoder_2 = self.ReLU(self.conv2(concat2))  # 128
        upsample4 = self._block4(decoder_2)  # 64
        upsample4 = self.MSRB3(upsample4)

        beta_3 = self.conv3_1(swin_out_1[0])
        gamma_3 = self.conv3_2(swin_out_1[0])
        swin_input_1_refine = self.IN_1(swin_input_1) * beta_3 + gamma_3  # 32
        concat1 = torch.cat((swin_input_1, swin_input_1_refine, upsample4), dim=1)  # 64+64+64=128
        decoder_1 = self.ReLU(self.conv3(concat1))  # 64
        upsample5 = self._block5(decoder_1)  # 32
        # upsample5=self.MSRB4(upsample5)
        # decoder_0_1 = self.ReLU(self.conv4_1(swin_out_1[0]))#48
        # decoder_0_2 = self.ReLU(self.conv4_2(decoder_0_1))#48
        # decoder_0_3 = self.ReLU(self.conv4_3(decoder_0_2))#24
        # decoder_0_4 = self.ReLU(self.conv4_4(decoder_0_3))#12
        # concat0 = torch.cat((upsample5,decoder_0_1), dim=1)#48+48=96
        decoder_0 = self.ReLU(self.conv4(upsample5))  # 48
        # upsample6 = self._block6(decoder_0)#48

        # Refine_1=self._block6(decoder_0)
        result = self._block7(decoder_0)  # 23
        # result=self._block7(self._block6(Refine_2))
        # concat2 = torch.cat((upsample2, swin_out), dim=1)
        # concat2 = upsample2
        # upsample0 = self._block5(upsample1)
        # concat1 = torch.cat((upsample1, x, swin_in), dim=1)

        # Final activation
        return result


class PPM(nn.Module):
    def __init__(self, in_dim, reduction_dim, bins):
        super(PPM, self).__init__()
        self.features = []
        for bin in bins:
            self.features.append(nn.Sequential(
                nn.AdaptiveAvgPool2d(bin),
                nn.Conv2d(in_dim, reduction_dim, kernel_size=1, bias=False),
                # nn.BatchNorm2d(reduction_dim),
                nn.ReLU(inplace=True)
            ))
        self.features = nn.ModuleList(self.features)

    def forward(self, x):
        x_size = x.size()
        out = [x]
        for f in self.features:
            out.append(F.interpolate(f(x), x_size[2:], mode='bilinear', align_corners=True))  # what is f(x)
        return torch.cat(out, 1)


class UNet(nn.Module):
    """Custom U-Net architecture for Noise2Noise (see Appendix, Table 2)."""

    def __init__(self, in_channels=3, out_channels=3):
        """Initializes U-Net."""

        super(UNet, self).__init__()
        self.embedding_dim = 3
        self.conv0 = nn.Conv2d(3, self.embedding_dim, 3, stride=1, padding=1)
        self.conv1 = nn.Conv2d(768, 768, 3, stride=1, padding=1)
        self.conv2 = nn.Conv2d(384, 384, 3, stride=1, padding=1)
        self.conv3 = nn.Conv2d(192, 192, 3, stride=1, padding=1)
        self.conv4 = nn.Conv2d(96, 96, 3, stride=1, padding=1)
        self.in_chans = 3
        # 27,565,242
        self.swin = SwinTransformer(pretrain_img_size=224,
                                    patch_size=4,
                                    in_chans=self.in_chans,
                                    embed_dim=96,
                                    depths=[2, 2, 6, 2],
                                    num_heads=[3, 6, 12, 24],
                                    window_size=7,
                                    mlp_ratio=4.,
                                    qkv_bias=True,
                                    qk_scale=None,
                                    drop_rate=0.,
                                    attn_drop_rate=0.,
                                    drop_path_rate=0.2,
                                    norm_layer=nn.LayerNorm,
                                    ape=False,
                                    patch_norm=True,
                                    out_indices=(0, 1, 2, 3),
                                    frozen_stages=-1,
                                    use_checkpoint=False)
        # self.avg_pool = nn.AdaptiveAvgPool2d(1)

        self._block1 = nn.Sequential(
            nn.Conv2d(768, 768, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(768, 768, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.ConvTranspose2d(768, 384, 3, stride=2, padding=1, output_padding=1))

        self._block2 = nn.Sequential(
            nn.Conv2d(768, 384, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(384, 384, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.ConvTranspose2d(384, 192, 3, stride=2, padding=1, output_padding=1))

        self._block3 = nn.Sequential(
            nn.Conv2d(384, 192, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(192, 192, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.ConvTranspose2d(192, 96, 3, stride=2, padding=1, output_padding=1))

        self._block4 = nn.Sequential(
            nn.Conv2d(192, 96, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(96, 96, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.ConvTranspose2d(96, 96, 3, stride=2, padding=1, output_padding=1))

        self._block5 = nn.Sequential(
            nn.Conv2d(96, 96, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(96, 96, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.ConvTranspose2d(96, 96, 3, stride=2, padding=1, output_padding=1))

        self._block6 = nn.Sequential(
            nn.Conv2d(96 + in_channels + self.embedding_dim, 64, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(64, 32, 3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, out_channels, 3, stride=1, padding=1),
            nn.LeakyReLU(0.1))

        # Initialize weights
        self._init_weights()
        print('the number of swin parameters: {}'.format(
            sum([p.data.nelement() for p in self.swin.parameters()])))

    def _init_weights(self):
        """Initializes weights using He et al. (2015)."""

        for m in self.modules():
            if isinstance(m, nn.ConvTranspose2d) or isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight.data)
                m.bias.data.zero_()

    def forward(self, x):
        """Through encoder, then decoder by adding U-skip connections. """
        # swin_in = self.conv0(x)
        swin_in = x
        swin_out = self.swin(swin_in)

        # Decoder
        swin_out_3 = self.conv1([3])
        upsample5 = self._block1(swin_out_3)

        swin_out_2 = self.conv2(swin_out[2])
        concat5 = torch.cat((upsample5, swin_out_2), dim=1)
        upsample4 = self._block2(concat5)

        swin_out_1 = self.conv3(swin_out[1])
        concat4 = torch.cat((upsample4, swin_out_1), dim=1)
        upsample3 = self._block3(concat4)

        swin_out_0 = self.conv4(swin_out[0])
        concat3 = torch.cat((upsample3, swin_out_0), dim=1)
        upsample2 = self._block4(concat3)

        # concat2 = torch.cat((upsample2, swin_out), dim=1)
        concat2 = upsample2
        upsample1 = self._block5(concat2)
        concat1 = torch.cat((upsample1, x, swin_in), dim=1)

        # Final activation
        return self._block6(concat1)

class TexturePath(nn.Module):
    def __init__(self):
        super(TexturePath, self).__init__()
        self.conv1 = ConvBNReLU(3, 64, ks=7, stride=2, padding=3)
        self.conv2 = ConvBNReLU(64, 64, ks=3, stride=2, padding=1)
        self.conv3 = ConvBNReLU(64, 64, ks=3, stride=2, padding=1)
        self.conv_out = ConvBNReLU(64, 128, ks=1, stride=1, padding=0)
        self.init_weight()

    def forward(self, x):
        feat = self.conv1(x)
        feat = self.conv2(feat)
        feat = self.conv3(feat)
        feat = self.conv_out(feat)
        return feat

    def init_weight(self):
        for ly in self.children():
            if isinstance(ly, nn.Conv2d):
                nn.init.kaiming_normal_(ly.weight, a=1)
                if not ly.bias is None: nn.init.constant_(ly.bias, 0)

    def get_params(self):
        wd_params, nowd_params = [], []
        for name, module in self.named_modules():
            if isinstance(module, nn.Linear) or isinstance(module, nn.Conv2d):
                wd_params.append(module.weight)
                if not module.bias is None:
                    nowd_params.append(module.bias)
            elif isinstance(module, nn.modules.batchnorm._BatchNorm):
                nowd_params += list(module.parameters())
        return wd_params, nowd_params


class DependencyPath(nn.Module):
    def __init__(self, weight_path='pretrain_weights/rest_lite.pth'):
        super(DependencyPath, self).__init__()
        self.ResT = rest_lite(weight_path=weight_path)
        self.AE = Attention_Embedding(512, 256)
        self.conv_avg = ConvBNReLU(256, 128, ks=1, stride=1, padding=0)
        self.up = nn.Upsample(scale_factor=2.)

    def forward(self, x):
        e3, e4 = self.ResT(x)

        e = self.conv_avg(self.AE(e4, e3))

        return self.up(e)

    def get_params(self):
        wd_params, nowd_params = [], []
        for name, module in self.named_modules():
            if isinstance(module, (nn.Linear, nn.Conv2d)):
                wd_params.append(module.weight)
                if not module.bias is None:
                    nowd_params.append(module.bias)
            elif isinstance(module, nn.modules.batchnorm._BatchNorm):
                nowd_params += list(module.parameters())
        return wd_params, nowd_params


class DependencyPathRes(nn.Module):
    def __init__(self):
        super(DependencyPathRes, self).__init__()
        resnet = models.resnet18(True)
        self.firstconv = resnet.conv1
        self.firstbn = resnet.bn1
        self.firstrelu = resnet.relu
        self.firstmaxpool = resnet.maxpool
        self.encoder1 = resnet.layer1
        self.encoder2 = resnet.layer2
        self.encoder3 = resnet.layer3
        self.encoder4 = resnet.layer4
        self.AE = Attention_Embedding(512, 256)
        self.conv_avg = ConvBNReLU(256, 128, ks=1, stride=1, padding=0)
        self.up = nn.Upsample(scale_factor=2.)

    def forward(self, x):
        x1 = self.firstconv(x)
        x1 = self.firstbn(x1)
        x1 = self.firstrelu(x1)
        x1 = self.firstmaxpool(x1)
        e1 = self.encoder1(x1)
        e2 = self.encoder2(e1)
        e3 = self.encoder3(e2)
        e4 = self.encoder4(e3)

        e = self.conv_avg(self.AE(e4, e3))

        return self.up(e)

    def get_params(self):
        wd_params, nowd_params = [], []
        for name, module in self.named_modules():
            if isinstance(module, (nn.Linear, nn.Conv2d)):
                wd_params.append(module.weight)
                if not module.bias is None:
                    nowd_params.append(module.bias)
            elif isinstance(module, nn.modules.batchnorm._BatchNorm):
                nowd_params += list(module.parameters())
        return wd_params, nowd_params


class BTCNet(nn.Module):
    def __init__(self, num_classes=6, weight_path='pretrain_weights/rest_lite.pth'):
        super(BTCNet, self).__init__()    # path of pretrained weight file of ResT-lite  or None, recommend use.
        self.name = 'BTCNet'

        self.cp = DependencyPath(weight_path=weight_path)
        self.sp = TexturePath()
        self.fam = FeatureAggregationModule(256, 256)
        self.conv_out = Output(256, 256, num_classes, up_factor=8)
        self.init_weight()

    def forward(self, x):
        feat = self.cp(x)
        feat_sp = self.sp(x)
        feat_fuse = self.fam(feat_sp, feat)

        feat_out = self.conv_out(feat_fuse)

        return feat_out

    def init_weight(self):
        for ly in self.children():
            if isinstance(ly, nn.Conv2d):
                nn.init.kaiming_normal_(ly.weight, a=1)
                if not ly.bias is None: nn.init.constant_(ly.bias, 0)

    def get_params(self):
        wd_params, nowd_params, lr_mul_wd_params, lr_mul_nowd_params = [], [], [], []
        for name, child in self.named_children():
            child_wd_params, child_nowd_params = child.get_params()
            if isinstance(child, (Attention_Embedding, Output)):
                lr_mul_wd_params += child_wd_params
                lr_mul_nowd_params += child_nowd_params
            else:
                wd_params += child_wd_params
                nowd_params += child_nowd_params
        return wd_params, nowd_params, lr_mul_wd_params, lr_mul_nowd_params