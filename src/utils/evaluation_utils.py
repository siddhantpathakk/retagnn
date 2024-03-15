import warnings
warnings.filterwarnings("ignore")

import math
import torch
import logging
import numpy as np
import multiprocessing
from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import average_precision_score, f1_score, roc_auc_score


def recall(rank, ground_truth, N):
    return len(set(rank[:N]) & set(ground_truth)) / float(len(set(ground_truth)))


def precision_at_k(r, k):
    """Score is precision @ k
    Relevance is binary (nonzero is relevant).
    Returns:
        Precision @ k
    Raises:
        ValueError: len(r) must be >= k
    """
    assert k >= 1
    r = np.asarray(r)[:k]
    return np.mean(r)


def average_precision(r,cut):
    """Score is average precision (area under PR curve)
    Relevance is binary (nonzero is relevant).
    Returns:
        Average precision
    """
    r = np.asarray(r)
    out = [precision_at_k(r, k + 1) for k in range(cut) if r[k]]
    if not out:
        return 0.
    return np.sum(out)/float(min(cut, np.sum(r)))


def mean_average_precision(rs):
    """Score is mean average precision
    Relevance is binary (nonzero is relevant).
    Returns:
        Mean average precision
    """
    return np.mean([average_precision(r) for r in rs])


def dcg_at_k(r, k, method=1):
    """Score is discounted cumulative gain (dcg)
    Relevance is positive real values.  Can use binary
    as the previous methods.
    Returns:
        Discounted cumulative gain
    """
    r = np.asfarray(r)[:k]
    if r.size:
        if method == 0:
            return r[0] + np.sum(r[1:] / np.log2(np.arange(2, r.size + 1)))
        elif method == 1:
            return np.sum(r / np.log2(np.arange(2, r.size + 2)))
        else:
            raise ValueError('method must be 0 or 1.')
    return 0.


def ndcg_at_k(r, k, method=1):
    """Score is normalized discounted cumulative gain (ndcg)
    Relevance is positive real values.  Can use binary
    as the previous methods.
    Returns:
        Normalized discounted cumulative gain
    """
    dcg_max = dcg_at_k(sorted(r, reverse=True), k, method)
    if not dcg_max:
        return 0.
    return dcg_at_k(r, k, method) / dcg_max




def hit_at_k(r, k):
    r = np.array(r)[:k]
    if np.sum(r) > 0:
        return 1.
    else:
        return 0.

def F1(pre, rec):
    if pre + rec > 0:
        return (2.0 * pre * rec) / (pre + rec)
    else:
        return 0.

def area_under_curve(ground_truth, prediction):
    try:
        res = roc_auc_score(y_true=ground_truth, y_score=prediction)
    except Exception:
        res = 0.
    return res

def recall_at_k(r, k, all_pos_num):
    r = np.asfarray(r)[:k]
    return np.sum(r) / all_pos_num

def mean_reciprocal_rank(r):
    r = np.array(r)
    if np.sum(r) == 0:
        return 0.
    return np.reciprocal(np.where(r==1)[0]+1, dtype=np.float64)[0]


Ks = [10, 20]

def eval_one_user(x):    
    result = {
              'recall': np.zeros(len(Ks)), 
              'ndcg': np.zeros(len(Ks)),
                'mrr': 0.}
    
    preds = np.transpose(x[0])
    num_preditems = x[1]

    num_neg_sample_items = x[2]
    num_candidate_items = x[3]

    labels = np.zeros(num_preditems)
    labels[0] = 1
    r = []
    rankeditems = np.argsort(-preds)[:max(Ks)]
    for i in rankeditems:
        if i == 0:
            r.append(1)
        else:
            r.append(0)
    if num_neg_sample_items != -1:
        r = rank_corrected(np.array(r), num_preditems, num_candidate_items)

    recall, ndcg = [], []
    for K in Ks:
        recall.append(recall_at_k(r, K, 1))
        ndcg.append(ndcg_at_k(r, K))
    mrr = mean_reciprocal_rank(r)


    result['recall'] += recall
    result['ndcg'] += ndcg
    result['mrr'] += mrr
    return result


def rank_corrected(r, m, n):
    pos_ranks = np.argwhere(r==1)[:,0]
    corrected_r = np.zeros_like(r)
    for each_sample_rank in list(pos_ranks):
        corrected_rank = int(np.floor(((n-1)*each_sample_rank)/m))
        if corrected_rank >= len(corrected_r) - 1:
            continue
        corrected_r[corrected_rank] = 1
    assert sum(corrected_r) <= 1
    return corrected_r


def eval_users(tgrec, src, dst, ts, train_src, train_dst, args):
    result = {
              'recall': np.zeros(len(Ks)), 
              'ndcg': np.zeros(len(Ks)),
                'mrr': 0.}
    
    cores = multiprocessing.cpu_count() // 2
    train_itemset = set(train_dst)
    pos_edges = {}
    
    for u, i, t in zip(src, dst, ts):
        if i not in train_itemset:
            continue
        if u in pos_edges:
            pos_edges[u].add((i, t))
        else:
            pos_edges[u] = set([(i, t)])
            
    train_pos_edges = {}
    for u, i in zip(train_src, train_dst):
        if u in train_pos_edges:
            train_pos_edges[u].add(i)
        else:
            train_pos_edges[u] = set([i])

    pool = multiprocessing.Pool(cores)
    batch_users = 1000

    preds_list, preds_len_preditems, preds_sampled_neg, preds_num_candidates = [], [], [], []


    num_interactions,num_test_instances = 0, 0

    with torch.no_grad():
        tgrec = tgrec.eval()
        batch_src_l = []
        batch_test_items = []
        batch_ts = []
        batch_i = 0
        
        for u, i, t in zip(src, dst, ts):
            
            num_test_instances += 1
            if u not in train_src or i not in train_itemset or u not in pos_edges:
                continue
            num_interactions += 1
            batch_i += 1

            pos_items = [i]
            pos_ts = [t]
            src_l = [u for _ in range(len(pos_items))]

            interacted_dst = train_pos_edges[u]

            neg_candidates = list(train_itemset - set(pos_items) - interacted_dst)
            if args.negsampleeval == -1:
                neg_items = neg_candidates
            else:
                neg_items = list(np.random.choice(neg_candidates, size=args.negsampleeval, replace=False))

            neg_ts = [t for _ in range(len(neg_items))]
            neg_src_l = [u for _ in range(len(neg_items))]

            batch_src_l += src_l + neg_src_l
            batch_test_items += pos_items + neg_items
            batch_ts += pos_ts + neg_ts

            test_items = np.array(batch_test_items)
            test_ts = np.array(batch_ts)
            test_src_l = np.array(batch_src_l)

            pred_scores = tgrec(test_src_l, test_items, test_ts, args.n_degree)
            preds = pred_scores.cpu().numpy()

            preds_list.append(preds)
            preds_len_preditems.append(len(src_l+neg_src_l))
            preds_sampled_neg.append(args.negsampleeval)
            preds_num_candidates.append(len(pos_items+neg_candidates))
            batch_src_l = []
            batch_test_items = []
            batch_ts = []

            if len(preds_list) % batch_users == 0 or num_test_instances == len(ts):

                batchset_predictions = zip(preds_list, preds_len_preditems, preds_sampled_neg, preds_num_candidates)
                batch_preds = pool.map(eval_one_user, batchset_predictions)
                for oneresult in batch_preds:
                    result['recall'] += oneresult['recall']
                    result['ndcg'] += oneresult['ndcg']
                    result['mrr'] += oneresult['mrr']
                    # print(result)

                preds_list, preds_len_preditems, preds_sampled_neg, preds_num_candidates = [], [], [], []
                batch_src_l, batch_test_items,batch_ts = [], [], []

    # print(result)
    result['recall'] /= num_interactions
    result['ndcg'] /= num_interactions
    result['mrr'] /= num_interactions
    # print(result)
    return result

def eval_one_epoch(hint, tgrec, sampler, src, dst, ts, label, NUM_NEIGHBORS=20):
    val_acc, val_ap, val_f1, val_auc = [], [], [], []
    with torch.no_grad():
        tgrec = tgrec.eval()
        TEST_BATCH_SIZE=1024
        num_test_instance = len(src)
        num_test_batch = math.ceil(num_test_instance / TEST_BATCH_SIZE)
        for k in range(num_test_batch):
            # percent = 100 * k / num_test_batch
            # if k % int(0.2 * num_test_batch) == 0:
            #     logger.info('{0} progress: {1:10.4f}'.format(hint, percent))
            s_idx = k * TEST_BATCH_SIZE
            e_idx = min(num_test_instance - 1, s_idx + TEST_BATCH_SIZE)
            src_l_cut = src[s_idx:e_idx]
            dst_l_cut = dst[s_idx:e_idx]
            ts_l_cut = ts[s_idx:e_idx]
            # label_l_cut = label[s_idx:e_idx]

            size = len(src_l_cut)
            dst_l_fake = sampler.sample_neg(src_l_cut)

            pos_prob, neg_prob = tgrec.contrast(src_l_cut, dst_l_cut, dst_l_fake, ts_l_cut, NUM_NEIGHBORS)
            
            pred_score = np.concatenate([(pos_prob).cpu().numpy(), (neg_prob).cpu().numpy()])
            pred_label = pred_score > 0.5
            true_label = np.concatenate([np.ones(size), np.zeros(size)])
            
            val_acc.append((pred_label == true_label).mean())
            val_ap.append(average_precision_score(true_label, pred_score))
            val_f1.append(f1_score(true_label, pred_label))
            val_auc.append(roc_auc_score(true_label, pred_score))
            
    return np.mean(val_acc), np.mean(val_ap), np.mean(val_f1), np.mean(val_auc)
