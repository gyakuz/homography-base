from typing import Dict, Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops.einops import rearrange


# INF = 1e9


def compute_max_candidates(p_m0, p_m1):
    """Compute the max candidates of all pairs within a batch
    
    Args:
        p_m0, p_m1 (torch.Tensor): padded masks
    """
    h0s, w0s = p_m0.sum(1).max(-1)[0], p_m0.sum(-1).max(-1)[0]
    h1s, w1s = p_m1.sum(1).max(-1)[0], p_m1.sum(-1).max(-1)[0]
    max_cand = torch.sum(
        torch.min(torch.stack([h0s * w0s, h1s * w1s], -1), -1)[0])
    return max_cand


class CoarseMatching(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        # general loftr_config
        self.thr = config['thr']
        self.border_rm = config['border_rm']
        self.border_rm = 0
        # -- # for trainig fine-level LoFTR
        self.train_coarse_percent = config['train_coarse_percent']
        self.train_pad_num_gt_min = config['train_pad_num_gt_min']

        # we provide 2 options for differentiable matching
        self.match_type = config['match_type']
        if self.match_type == 'dual_softmax':
            self.temperature = config['dsmax_temperature']
        elif self.match_type == 'sinkhorn':
            try:
                from .superglue import log_optimal_transport
            except ImportError:
                raise ImportError("download superglue.py first!")
            self.log_optimal_transport = log_optimal_transport
            self.bin_score = nn.Parameter(
                torch.tensor(config['skh_init_bin_score'], requires_grad=True))
            self.skh_iters = config['skh_iters']
            self.skh_prefilter = config['skh_prefilter']
        else:
            raise NotImplementedError()

    def mask_border_with_padding(self, m, bd: int, v: bool, p_m0, p_m1):
        if bd <= 0:
            return

        m[:, :bd] = v
        m[:, :, :bd] = v
        m[:, :, :, :bd] = v
        m[:, :, :, :, :bd] = v

        h0s, w0s = p_m0.sum(1).max(-1)[0].int(), p_m0.sum(-1).max(-1)[0].int()
        h1s, w1s = p_m1.sum(1).max(-1)[0].int(), p_m1.sum(-1).max(-1)[0].int()
        for b_idx, (h0, w0, h1, w1) in enumerate(zip(h0s, w0s, h1s, w1s)):
            m[b_idx, h0 - bd:] = v
            m[b_idx, :, w0 - bd:] = v
            m[b_idx, :, :, h1 - bd:] = v
            m[b_idx, :, :, :, w1 - bd:] = v

    def mask_border(self, m, b: int, v: bool):
        """ Mask borders with value
        Args:
            m (torch.Tensor): [N, H0, W0, H1, W1]
            b (int)
            v (m.dtype)
        """
        if b <= 0:
            return

        m[:, :b] = v
        m[:, :, :b] = v
        m[:, :, :, :b] = v
        m[:, :, :, :, :b] = v
        m[:, -b:] = v
        m[:, :, -b:] = v
        m[:, :, :, -b:] = v
        m[:, :, :, :, -b:] = v

    def forward(self, feat_c0, feat_c1, data: Dict[str, torch.Tensor], mask_c0: Optional[torch.Tensor] = None,
                mask_c1: Optional[torch.Tensor] = None):
        """
        Args:
            feat0 (torch.Tensor): [N, L, C]
            feat1 (torch.Tensor): [N, S, C]
            data (dict)
            mask_c0 (torch.Tensor): [N, L] (optional)
            mask_c1 (torch.Tensor): [N, S] (optional)
        Update:
            data (dict): {
                'b_ids' (torch.Tensor): [M'],
                'i_ids' (torch.Tensor): [M'],
                'j_ids' (torch.Tensor): [M'],
                'gt_mask' (torch.Tensor): [M'],
                'mkpts0_c' (torch.Tensor): [M, 2],
                'mkpts1_c' (torch.Tensor): [M, 2],
                'mconf' (torch.Tensor): [M]}
            NOTE: M' != M during training.
        """
        N, L, S, C = feat_c0.size(0), feat_c0.size(1), feat_c1.size(1), feat_c0.size(2)

        # normalize
        feat_c0, feat_c1 = feat_c0 / feat_c0.shape[-1] ** .5, feat_c1 / feat_c1.shape[-1] ** .5
        # feat_c0, feat_c1 = map(lambda feat: feat / feat.shape[-1]**.5,
        #                        [feat_c0, feat_c1])
        INF = 1e9
        # if self.match_type == 'dual_softmax':
        sim_matrix = torch.einsum("nlc,nsc->nls", feat_c0,
                                  feat_c1) / self.temperature
        if mask_c0 is not None:
            assert mask_c0 is not None and mask_c1 is not None
            # sim_matrix[~(mask_c0[..., None] * mask_c1[:, None])] = -torch.tensor(INF)
            sim_matrix.masked_fill_(
                ~(mask_c0[..., None] * mask_c1[:, None]), -INF)
        conf_matrix = F.softmax(sim_matrix, 1) * F.softmax(sim_matrix, 2)

        data.update({'conf_matrix': conf_matrix})

        # predict coarse matches from conf_matrix
        data.update(self.get_coarse_match(conf_matrix, data))

    @torch.no_grad()
    def get_coarse_match(self, conf_matrix, data: Dict[str, torch.Tensor]):
        """
        Args:
            conf_matrix (torch.Tensor): [N, L, S]
            data (dict): with keys ['hw0_i', 'hw1_i', 'hw0_c', 'hw1_c']
        Returns:
            coarse_matches (dict): {
                'b_ids' (torch.Tensor): [M'],
                'i_ids' (torch.Tensor): [M'],
                'j_ids' (torch.Tensor): [M'],
                'gt_mask' (torch.Tensor): [M'],
                'm_bids' (torch.Tensor): [M],
                'mkpts0_c' (torch.Tensor): [M, 2],
                'mkpts1_c' (torch.Tensor): [M, 2],
                'mconf' (torch.Tensor): [M]}
        """
        # axes_lengths = {
        #     'h0c': data['hw0_c'][0],
        #     'w0c': data['hw0_c'][1],
        #     'h1c': data['hw1_c'][0],
        #     'w1c': data['hw1_c'][1]
        # }
        h0c = data['hw0_c'][0]
        w0c = data['hw0_c'][1]
        h1c = data['hw1_c'][0]
        w1c = data['hw1_c'][1]
        _device = conf_matrix.device
        # 1. confidence thresholding
        mask = conf_matrix > self.thr
        b = mask.shape[0]
        mask = mask.reshape(b, h0c, w0c, h1c, w1c)
        # mask = rearrange(mask, 'b (h0c w0c) (h1c w1c) -> b h0c w0c h1c w1c',
        #                  **axes_lengths)
        if 'mask0' not in data:
            self.mask_border(mask, self.border_rm, False)
        else:
            self.mask_border_with_padding(mask, self.border_rm, False,
                                          data['mask0'], data['mask1'])
        # mask = rearrange(mask, 'b h0c w0c h1c w1c -> b (h0c w0c) (h1c w1c)',
        #                  **axes_lengths)
        mask = mask.reshape(b, h0c * w0c, h1c * w1c)

        # 2. mutual nearest
        mask = mask \
               * (conf_matrix == conf_matrix.max(dim=2, keepdim=True)[0]) \
               * (conf_matrix == conf_matrix.max(dim=1, keepdim=True)[0])

        # 3. find all valid coarse matches
        # this only works when at most one `True` in each row
        if 'dataset_name' in data:
            ck = mask.view(b, -1).sum(-1) == 0
            mask[ck, 0, 0] = True
        mask_v, all_j_ids = mask.max(dim=2)
        b_ids, i_ids = torch.where(mask_v)
        j_ids = all_j_ids[b_ids, i_ids]
        mconf = conf_matrix[b_ids, i_ids, j_ids]

        coarse_matches = {'b_ids': b_ids, 'i_ids': i_ids, 'j_ids': j_ids}

        # 4. Update with matches in original image resolution
        scale = data['hw0_i'][0] / data['hw0_c'][0]
        scale0 = scale * data['scale0'][b_ids] if 'scale0' in data else scale
        scale1 = scale * data['scale1'][b_ids] if 'scale1' in data else scale
        mkpts0_c = torch.stack(
            [i_ids % data['hw0_c'][1], i_ids // data['hw0_c'][1]],
            dim=1) * scale0
        mkpts1_c = torch.stack(
            [j_ids % data['hw1_c'][1], j_ids // data['hw1_c'][1]],
            dim=1) * scale1

        # These matches is the current prediction (for visualization)
        coarse_matches.update({
            # 'gt_mask': mconf == 0,
            'm_bids': b_ids,
            'mkpts0_c': mkpts0_c,
            'mkpts1_c': mkpts1_c,
            'mconf': mconf
        })

        return coarse_matches
