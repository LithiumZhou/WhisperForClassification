import numpy as np
import torch
import math
import torch.nn.functional as F
from torch import nn
from whisper_at.model import ResidualAttentionBlock, Linear
from base_module import CT_MSA
import re


def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to
        # [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor

def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)


class TLTR(nn.Module):
    def __init__(self, label_dim=527, n_layer=33, rep_dim=1280, mode='basic',drop=0.):
        super().__init__()
        self.mode = mode
        self.n_layer = n_layer
        self.rep_dim = rep_dim
        self.label_dim = label_dim
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        # (baseline) mean pool over time and layer, and mlp head
        if mode == 'mean_mlp' or mode == 'last_mlp':
            self.mlp_layer = nn.Sequential(nn.LayerNorm(self.rep_dim), nn.Linear(self.rep_dim, self.label_dim))

        if mode == 'wa_mlp':
            self.mlp_layer = nn.Sequential(nn.LayerNorm(self.rep_dim), nn.Linear(self.rep_dim, self.label_dim))
            self.layer_weight = torch.nn.Parameter(torch.tensor([1 / self.n_layer] * self.n_layer))

        # (baseline) weight average over l ayers, and apply a original rep_dim transformer
        if 'wa_tr' in mode:
            self.num_att_head = int(mode.split('_')[-1])
            self.layer_weight = torch.nn.Parameter(torch.tensor([1 / self.n_layer] * self.n_layer)) # [32] every item is 1 / self.n_layer
            self.time_tr = ResidualAttentionBlock(self.rep_dim, self.num_att_head)
            self.mlp_layer = nn.Sequential(nn.LayerNorm(self.rep_dim), nn.Linear(self.rep_dim, self.label_dim))

        # (proposed), tl-tr with low-dimension projection, lower the dimension of the transformer # lw_down_tr_512_1_8
        if 'lw_down_ctr' in mode:
            self.inter_rep_dim = int(mode.split('_')[-3])
            self.num_tatt_head = int(mode.split('_')[-2])
            self.num_latt_head = int(mode.split('_')[-1])

            self.down_layer = nn.Sequential(nn.LayerNorm(self.rep_dim), nn.Linear(self.rep_dim, self.inter_rep_dim))
            self.time_tr = CT_MSA(self.inter_rep_dim,heads=self.num_tatt_head,window_size=[5,10,20],num_time=100,depth=3,device=self.device,causal=False,pos=False)
            self.layer_tr = ResidualAttentionBlock(self.inter_rep_dim, self.num_latt_head)
            self.mlp_layer = nn.Sequential(nn.LayerNorm(self.inter_rep_dim), nn.Linear(self.inter_rep_dim, self.label_dim))

        elif 'wa_ctr' in mode:
            numbers = re.findall(r'\d+', mode)
            numbers_list = [int(num) for num in numbers]
            self.num_att_head = numbers_list[0]
            self.window_size = numbers_list[1:]
            self.depth = len(self.window_size)
            self.layer_weight = torch.nn.Parameter(torch.tensor([1 / self.n_layer] * self.n_layer))
            self.ctr = CT_MSA(self.rep_dim,heads=self.num_att_head,window_size=self.window_size,num_time=250,depth=self.depth,device=self.device,causal=False,pos=False)
            self.mlp_layer = nn.Sequential(nn.LayerNorm(self.rep_dim), nn.Linear(self.rep_dim, self.label_dim))

    def forward(self, audio_rep):
        # audio_rep in shape (# batch size, #whisper_enc_layer, time length after (20x) pooling, whisper_enc_dim)
        # e.g., (B, 32, 25, 1280) for whisper large-v1

        # (baseline) time transformer on the layer-wise weight-average representation

        if self.mode == 'last_mlp':
            audio_rep = audio_rep[:, -1, :, :] # get the last layer
            audio_rep = torch.mean(audio_rep, dim=1)
            audio_rep = self.mlp_layer(audio_rep)
            return audio_rep

        elif self.mode == 'wa_mlp':
            audio_rep = torch.mean(audio_rep, dim=2) # [B, 32 1280]
            audio_rep = torch.permute(audio_rep, (0, 2, 1)) # (B, 1280, 32)
            audio_rep = (audio_rep @ self.layer_weight) / self.layer_weight.sum()
            audio_rep = self.mlp_layer(audio_rep)
            return audio_rep

        elif 'wa_tr' in self.mode:
            audio_rep = torch.permute(audio_rep, (0, 2, 3, 1)) # (B, 25, 1280, 32)
            audio_rep = (audio_rep @ self.layer_weight) / self.layer_weight.sum() # [B, 25, 1280]
            audio_rep = self.time_tr(audio_rep) # [B, 25, 1280]
            embedding = audio_rep
            audio_rep = torch.mean(audio_rep, dim=1)  # [B, 1280]
            _audio_rep = audio_rep
            audio_rep = self.mlp_layer(audio_rep)
            return audio_rep

        elif 'lw_down_ctr' in self.mode:
            B = audio_rep.shape[0]
            audio_rep = self.down_layer(audio_rep)
            audio_rep = audio_rep.reshape(B*self.n_layer, audio_rep.shape[2], audio_rep.shape[3]) # [B*32, 25, 512]
            audio_rep = self.time_tr(audio_rep) # [B*32, 25, 512]
            audio_rep = torch.mean(audio_rep, dim=1) # [B*32, 512]
            audio_rep = audio_rep.reshape(B, self.n_layer, audio_rep.shape[1]) # [B, 32, 512]
            audio_rep = self.layer_tr(audio_rep) # [B, 32, 512]
            audio_rep = torch.mean(audio_rep, dim=1)  # [B, 512]
            audio_rep = self.mlp_layer(audio_rep)
            return audio_rep

        elif 'wa_ctr' in self.mode:
            audio_rep = torch.permute(audio_rep, (0, 2, 3, 1)) # (B, 25, 1280, 32)
            audio_rep = (audio_rep @ self.layer_weight) / self.layer_weight.sum() # [B, 25, 1280]
            embedding = audio_rep
            audio_rep = self.ctr(audio_rep) # B,25,1280
            audio_rep = torch.mean(audio_rep,dim=1)
            _audio_rep = audio_rep
            audio_rep = self.mlp_layer(audio_rep)
            return audio_rep
        
if __name__ == '__main__':
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    x = torch.randn([2,33,100,1280]).to(device)
    # m = TLTR(mode='mstf_tr_8',rep_dim=1280,n_layer=32)
    m = TLTR(mode='wa_tr_4')
    m.to(device)
    output = m(x)
    print(output.shape)





