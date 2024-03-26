import torch

from src.model.layers.attention.transformer import Transformer
from src.model.layers.encoding import EmptyEncode, PosEncode, TimeEncode
from src.model.layers.ffn import MergeLayer
from src.model.layers.pool import LSTMPool, MeanPool
torch.cuda.empty_cache()

import logging
import numpy as np
import torch.nn.functional as F

def expand_last_dim(x, num):
    view_size = list(x.size()) + [1]
    expand_size = list(x.size()) + [num]
    return x.view(view_size).expand(expand_size)

class TAGON(torch.nn.Module):
    def __init__(self, ngh_finder, n_nodes, args,
                 attn_mode='prod', use_time='time', agg_method='attn', node_dim=32, time_dim=32,
                 num_layers=3, n_head=4, null_idx=0, num_heads=1, drop_out=0.1, seq_len=None):
        super(TAGON, self).__init__()
        self.logger = logging.getLogger(__name__)
        
        self.num_layers = num_layers 
        self.ngh_finder = ngh_finder
        self.null_idx = null_idx
        self.node_hist_embed = torch.nn.Embedding(n_nodes, node_dim)
        torch.nn.init.uniform_(self.node_hist_embed.weight, a=-1.0, b=1.0)
        
        self.feat_dim = node_dim
        
        self.use_time = use_time

        self.n_feat_dim = node_dim
        self.model_dim = self.n_feat_dim + time_dim
        
        self.use_time = use_time
        self.time_att_weights = torch.nn.Parameter(torch.from_numpy(np.random.rand(node_dim, time_dim)).float())

        self.merge_layer = MergeLayer(self.feat_dim, self.feat_dim, self.feat_dim, self.feat_dim)
        
        if agg_method == 'attn':
            self.logger.info('Aggregation uses attention model')
            self.transformer = torch.nn.ModuleList([Transformer(args, self.n_feat_dim, 
                                                               time_dim,
                                                               attn_mode=attn_mode, 
                                                               n_head=n_head, 
                                                               drop_out=drop_out) for _ in range(num_layers)])
        elif agg_method == 'lstm':
            self.logger.info('Aggregation uses LSTM model')
            self.transformer = torch.nn.ModuleList([LSTMPool(self.n_feat_dim,
                                                                 time_dim) for _ in range(num_layers)])
        
        elif agg_method == 'mean':
            self.logger.info('Aggregation uses constant mean model')
            self.transformer = torch.nn.ModuleList([MeanPool(self.n_feat_dim) for _ in range(num_layers)])
        
        else:
            raise ValueError('invalid agg_method value, use attn or lstm')
        
        
        
        if use_time == 'time':
            self.logger.info('Using time encoding')
            self.time_encoder = TimeEncode(expand_dim=time_dim)
        
        elif use_time == 'pos':
            assert(seq_len is not None)
            self.logger.info('Using positional encoding')
            self.time_encoder = PosEncode(expand_dim=time_dim, seq_len=seq_len)
        
        elif use_time == 'empty':
            self.logger.info('Using empty encoding')
            self.time_encoder = EmptyEncode(expand_dim=time_dim)
        
        else:
            raise ValueError('invalid time option!')
        
        self.affinity_score = MergeLayer(self.feat_dim, self.feat_dim, self.feat_dim, 1) #torch.nn.Bilinear(self.feat_dim, self.feat_dim, 1, bias=True)
        
    def forward(self, src_idx_l, target_idx_l, cut_time_l, num_neighbors=20):
        src_embed = self.tem_conv(src_idx_l, cut_time_l, self.num_layers, num_neighbors)
        target_embed = self.tem_conv(target_idx_l, cut_time_l, self.num_layers, num_neighbors)
        return self.affinity_score(src_embed, target_embed).squeeze(dim=-1)

    def contrast(self, src_idx_l, target_idx_l, background_idx_l, cut_time_l, num_neighbors=20):
        src_embed = self.tem_conv(src_idx_l, cut_time_l, self.num_layers, num_neighbors)
        target_embed = self.tem_conv(target_idx_l, cut_time_l, self.num_layers, num_neighbors)
        background_embed = self.tem_conv(background_idx_l, cut_time_l, self.num_layers, num_neighbors)
        pos_score = self.affinity_score(src_embed, target_embed).squeeze(dim=-1)
        neg_score = self.affinity_score(src_embed, background_embed).squeeze(dim=-1)
        return pos_score.sigmoid(), neg_score.sigmoid()

    def contrast_nosigmoid(self, src_idx_l, target_idx_l, background_idx_l, cut_time_l, num_neighbors=20):
        src_embed = self.tem_conv(src_idx_l, cut_time_l, self.num_layers, num_neighbors)
        target_embed = self.tem_conv(target_idx_l, cut_time_l, self.num_layers, num_neighbors)
        background_embed = self.tem_conv(background_idx_l, cut_time_l, self.num_layers, num_neighbors)
        pos_score = self.affinity_score(src_embed, target_embed).squeeze(dim=-1)
        neg_score = self.affinity_score(src_embed, background_embed).squeeze(dim=-1)
        return pos_score, neg_score


    def time_att_aggregate(self, node_emb, node_time_emb):
        node_emb_to_time = torch.tensordot(node_emb, self.time_att_weights, dims=([-1], [0]))
        node_emb_to_time = torch.unsqueeze(node_emb_to_time, dim=-2)
        
        if len(node_emb.shape) == 2:
            node_emb_to_time = torch.unsqueeze(node_emb_to_time, dim=-2) 

        unnormalized_attentions = torch.sum(node_emb_to_time * node_time_emb, dim=-1) 
        normalized_attentions = F.softmax(unnormalized_attentions, dim=-1) 
        normalized_attentions = torch.unsqueeze(normalized_attentions, dim=-1)
        weighted_time_emb = torch.sum(normalized_attentions * node_time_emb, dim=-2)
        return weighted_time_emb


    def tem_conv(self, src_idx_l, cut_time_l, curr_layers, num_neighbors=20):
        device = torch.device('cuda:{}'.format(0))
        
        assert(curr_layers >= 0, 'Invalid layer number')
        
        batch_size = len(src_idx_l)
        src_node_batch_th = torch.from_numpy(src_idx_l).long().to(device)
        cut_time_l_th = torch.from_numpy(cut_time_l).float().to(device)
        
        cut_time_l_th = torch.unsqueeze(cut_time_l_th, dim=1)
        
        # query node always has the start time -> time span == 0
        src_node_t_embed = self.time_encoder(torch.zeros_like(cut_time_l_th))
        src_node_feat = self.node_hist_embed(src_node_batch_th)
        
        if curr_layers == 0:
            return src_node_feat
        
        else:
            src_node_conv_feat = self.tem_conv(src_idx_l, 
                                           cut_time_l,
                                           curr_layers=curr_layers - 1, 
                                           num_neighbors=num_neighbors)
            
            src_ngh_node_batch, _, src_ngh_t_batch = self.ngh_finder.get_temporal_neighbor(src_idx_l, cut_time_l, num_neighbors=num_neighbors)

            src_ngh_node_batch_th = torch.from_numpy(src_ngh_node_batch).long().to(device)
            
            src_ngh_t_batch_delta = cut_time_l[:, np.newaxis] - src_ngh_t_batch
            src_ngh_t_batch_th = torch.from_numpy(src_ngh_t_batch_delta).float().to(device)
            
            # get previous layer's node features
            src_ngh_node_batch_flat = src_ngh_node_batch.flatten() 
            src_ngh_t_batch_flat = src_ngh_t_batch.flatten() 
            src_ngh_node_conv_feat = self.tem_conv(src_ngh_node_batch_flat, 
                                                   src_ngh_t_batch_flat,
                                                   curr_layers=curr_layers - 1, 
                                                   num_neighbors=num_neighbors)
            src_ngh_feat = src_ngh_node_conv_feat.view(batch_size, num_neighbors, -1)
            
            # get edge time features and node features
            src_ngh_t_embed = self.time_encoder(src_ngh_t_batch_th)
            if self.use_time == 'disentangle':
                src_ngh_t_embed = self.time_att_aggregate(src_ngh_feat, src_ngh_t_embed)

            # attention aggregation
            mask = src_ngh_node_batch_th == 0
            transformer_layer = self.transformer[curr_layers - 1]
                        
            local, _ = transformer_layer(src_node_conv_feat, 
                                        src_node_t_embed,
                                        src_ngh_feat,
                                        src_ngh_t_embed, 
                                        mask)
            return local