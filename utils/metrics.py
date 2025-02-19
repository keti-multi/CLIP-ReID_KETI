import torch
import numpy as np
import os
from utils.reranking import re_ranking
from scipy.spatial import distance

def euclidean_distance(qf, gf):
    m = qf.shape[0]
    n = gf.shape[0]
    dist_mat = torch.pow(qf, 2).sum(dim=1, keepdim=True).expand(m, n) + \
               torch.pow(gf, 2).sum(dim=1, keepdim=True).expand(n, m).t()
    dist_mat.addmm_(1, -2, qf, gf.t())
    return dist_mat.cpu().numpy()
def scipy_euclidean_distance(qf, gf):
    dist = distance.cdist(qf,gf, metric='euclidean')
    return dist_mat.cpu().numpy()
def cosine_similarity(qf, gf):
    epsilon = 0.00001
    dist_mat = qf.mm(gf.t())
    qf_norm = torch.norm(qf, p=2, dim=1, keepdim=True)  # mx1
    gf_norm = torch.norm(gf, p=2, dim=1, keepdim=True)  # nx1
    qg_normdot = qf_norm.mm(gf_norm.t())

    dist_mat = dist_mat.mul(1 / qg_normdot).cpu().numpy()
    dist_mat = np.clip(dist_mat, -1 + epsilon, 1 - epsilon)
    dist_mat = np.arccos(dist_mat)
    return dist_mat


def eval_func(distmat, q_pids, g_pids, q_camids, g_camids, max_rank=50):
    """Evaluation with market1501 metric
        Key: for each query identity, its gallery images from the same camera view are discarded.
        """
    num_q, num_g = distmat.shape
    # distmat g
    #    q    1 3 2 4
    #         4 1 2 3
    if num_g < max_rank:
        max_rank = num_g
        print("Note: number of gallery samples is quite small, got {}".format(num_g))
    indices = np.argsort(distmat, axis=1)
    #  0 2 1 3
    #  1 2 3 0
    matches = (g_pids[indices] == q_pids[:, np.newaxis]).astype(np.int8)
    # compute cmc curve for each query
    all_cmc = []
    all_AP = []
    num_valid_q = 0.  # number of valid query

    CHECK_FALSE = True
    CHECK_TRUE = True

    if CHECK_FALSE:
        falser=[]
        falsee=[]
        falsee2=[]
        falsee3=[]
        falsee4=[]
        falsee5=[]
        cmc_keep=[]
        score_keep=[]
        succ_score_keep=[]
    if CHECK_TRUE :
        truer=[]
        truee=[]
        truee2=[]
        truee3=[]
        truee4=[]
        truee5=[]

        true_cmc_keep=[]
        true_score_keep=[]
        succ_score_keep=[]

    for q_idx in range(num_q):
        # get query pid and camid
        q_pid = q_pids[q_idx]
        q_camid = q_camids[q_idx]

        # remove gallery samples that have the same pid and camid with query
        order = indices[q_idx]  # select one row
        remove = (g_pids[order] == q_pid) & (g_camids[order] == q_camid)
        keep = np.invert(remove)

        # compute cmc curve
        # binary vector, positions with value 1 are correct matches
        orig_cmc = matches[q_idx][keep]
        if not np.any(orig_cmc):
            # this condition is true when query identity does not appear in gallery
            continue
        cmc = orig_cmc.cumsum()
        cmc[cmc > 1] = 1
        if cmc[0] != 1 and CHECK_FALSE:
            falser.append(q_idx)
            falsee.append(order[keep][0])
            falsee2.append(order[keep][1])
            falsee3.append(order[keep][2])
            falsee4.append(order[keep][3])
            falsee5.append(order[keep][4])
            cmc_keep.append(orig_cmc[:5])
            score_keep.append(distmat[q_idx][order[keep][:5]])

        # 230918 true test
        elif cmc[0]==1 and CHECK_TRUE:
            truer.append(q_idx)
            truee.append(order[keep][0])
            truee2.append(order[keep][1])
            truee3.append(order[keep][2])
            truee4.append(order[keep][3])
            truee5.append(order[keep][4])
            true_cmc_keep.append(orig_cmc[:5])
            true_score_keep.append(distmat[q_idx][order[keep][:5]])

        if cmc[0] == 1 and CHECK_FALSE:
            succ_score_keep.append(distmat[q_idx][order[keep][:5]])

        all_cmc.append(cmc[:max_rank])
        num_valid_q += 1.

        # compute average precision
        # reference: https://en.wikipedia.org/wiki/Evaluation_measures_(information_retrieval)#Average_precision
        num_rel = orig_cmc.sum()
        tmp_cmc = orig_cmc.cumsum()
        #tmp_cmc = [x / (i + 1.) for i, x in enumerate(tmp_cmc)]
        y = np.arange(1, tmp_cmc.shape[0] + 1) * 1.0
        tmp_cmc = tmp_cmc / y
        tmp_cmc = np.asarray(tmp_cmc) * orig_cmc
        AP = tmp_cmc.sum() / num_rel
        all_AP.append(AP)

    assert num_valid_q > 0, "Error: all query identities do not appear in gallery"
    if CHECK_FALSE:
        with open("utils/checks/fail_cmc_keep.txt", "w") as file:
            for row in cmc_keep:
                # Convert each row to a string of comma-separated values and add a newline character
                line = ",".join(map(str, row)) + "\n"
                file.write(line)
        with open("utils/checks/fail_score_keep.txt", "w") as file:
            for row in score_keep:
                # Convert each row to a string of comma-separated values and add a newline character
                line = ",".join(map("{:.8f}".format, row)) + "\n"
                file.write(line)
        with open("utils/checks/succ_score_keep.txt", "w") as file:
            for row in succ_score_keep:
                # Convert each row to a string of comma-separated values and add a newline character
                line = ",".join(map("{:.8f}".format, row)) + "\n"
                file.write(line)
        with open("utils/checks/clipreid_msmt_falser.txt", "w") as file:
            for i in range(len(falser)):
                file.write(
                    str(falser[i]) + "," + str(falsee[i]) + "," + str(falsee2[i]) + "," + str(falsee3[i]) + "," + str(
                        falsee4[i]) + "," + str(falsee5[i]) + "\n")
    if CHECK_TRUE:
        with open("utils/checks/true_cmc_keep.txt", "w") as file:
            for row in true_cmc_keep:
                # Convert each row to a string of comma-separated values and add a newline character
                line = ",".join(map(str, row)) + "\n"
                file.write(line)
        with open("utils/checks/true_score_keep.txt", "w") as file:
            for row in true_score_keep:
                # Convert each row to a string of comma-separated values and add a newline character
                line = ",".join(map("{:.8f}".format, row)) + "\n"
                file.write(line)
        with open("utils/checks/succ_score_keep.txt", "w") as file:
            for row in succ_score_keep:
                # Convert each row to a string of comma-separated values and add a newline character
                line = ",".join(map("{:.8f}".format, row)) + "\n"
                file.write(line)
        with open("utils/checks/clipreid_msmt_truer.txt", "w") as file:
            for i in range(len(truer)):
                file.write(
                    str(truer[i]) + "," + str(truee[i]) + "," + str(truee2[i]) + "," + str(truee3[i]) + "," + str(
                        truee4[i]) + "," + str(truee5[i]) + "\n")

    all_cmc = np.asarray(all_cmc).astype(np.float32)
    all_cmc = all_cmc.sum(0) / num_valid_q
    mAP = np.mean(all_AP)

    return all_cmc, mAP

def eval_func_all(distmat, q_pids, g_pids, q_camids, g_camids, max_rank=50):
    """Evaluation with market1501 metric
        Key: for each query identity, its gallery images from the same camera view are discarded.
        """
    num_q, num_g = distmat.shape
    # distmat g
    #    q    1 3 2 4
    #         4 1 2 3
    if num_g < max_rank:
        max_rank = num_g
        print("Note: number of gallery samples is quite small, got {}".format(num_g))
    indices = np.argsort(distmat, axis=1)
    #  0 2 1 3
    #  1 2 3 0
    matches = (g_pids[indices] == q_pids[:, np.newaxis]).astype(np.int8)
    # compute cmc curve for each query
    all_cmc = []
    all_AP = []
    num_valid_q = 0.  # number of valid query
    top_ind = []

    for q_idx in range(num_q):
        # get query pid and camid
        q_pid = q_pids[q_idx]
        q_camid = q_camids[q_idx]

        # remove gallery samples that have the same pid and camid with query
        order = indices[q_idx]  # select one row
        remove = (g_pids[order] == q_pid) & (g_camids[order] == q_camid)
        keep = np.invert(remove)

        # compute cmc curve
        # binary vector, positions with value 1 are correct matches
        orig_cmc = matches[q_idx][keep]
        for i in range(50):
            top_ind.append(order[keep][i])
        if not np.any(orig_cmc):
            # this condition is true when query identity does not appear in gallery
            # return 0,0,0,0
            continue
        cmc = orig_cmc.cumsum()
        cmc[cmc > 1] = 1

        all_cmc.append(cmc[:max_rank])
        num_valid_q += 1.

        # compute average precision
        # reference: https://en.wikipedia.org/wiki/Evaluation_measures_(information_retrieval)#Average_precision
        num_rel = orig_cmc.sum()
        tmp_cmc = orig_cmc.cumsum()
        #tmp_cmc = [x / (i + 1.) for i, x in enumerate(tmp_cmc)]
        y = np.arange(1, tmp_cmc.shape[0] + 1) * 1.0
        tmp_cmc = tmp_cmc / y
        tmp_cmc = np.asarray(tmp_cmc) * orig_cmc
        AP = tmp_cmc.sum() / num_rel
        all_AP.append(AP)

    assert num_valid_q > 0, "Error: all query identities do not appear in gallery"


    all_cmc = np.asarray(all_cmc).astype(np.float32)
    all_cmc = all_cmc.sum(0) / num_valid_q
    mAP = np.mean(all_AP)

    return all_cmc, mAP,top_ind,orig_cmc


class R1_mAP_eval():
    def __init__(self, num_query, max_rank=50, feat_norm=True, reranking=False):
        super(R1_mAP_eval, self).__init__()
        self.num_query = num_query
        self.max_rank = max_rank
        self.feat_norm = feat_norm
        self.reranking = reranking

    def reset(self):
        self.feats = []
        self.pids = []
        self.camids = []

    def update(self, output):  # called once for each batch
        feat, pid, camid = output
        self.feats.append(feat.cpu())
        self.pids.extend(np.asarray(pid))
        self.camids.extend(np.asarray(camid))

    def compute(self):  # called after each epoch
        feats = torch.cat(self.feats, dim=0)
        # feats = torch.load("msmt_trainset_feats.pt")
        # torch.save(feats,"msmt_trainset_feats.pt")
        # raise KeyboardInterrupt
        if self.feat_norm:
            print("The test feature is normalized")
            feats = torch.nn.functional.normalize(feats, dim=1, p=2)  # along channel
        # query
        qf = feats[:self.num_query]
        q_pids = np.asarray(self.pids[:self.num_query])
        q_camids = np.asarray(self.camids[:self.num_query])
        # gallery
        gf = feats[self.num_query:]
        g_pids = np.asarray(self.pids[self.num_query:])

        g_camids = np.asarray(self.camids[self.num_query:])
        if self.reranking:
            print('=> Enter reranking')
            # distmat = re_ranking(qf, gf, k1=20, k2=6, lambda_value=0.3)
            distmat = re_ranking(qf, gf, k1=50, k2=15, lambda_value=0.3)

        else:
            print('=> Computing DistMat with euclidean_distance')
            distmat = euclidean_distance(qf, gf)
        cmc, mAP = eval_func(distmat, q_pids, g_pids, q_camids, g_camids)

        return cmc, mAP, distmat, self.pids, self.camids, qf, gf

    # todo 230921 make msmt trainset validation
    def compute_train_all(self,logger):  # called after each epoch
        # feats = torch.cat(self.feats, dim=0)
        feats = torch.load("utils/checks/msmt_research/msmt_trainset_feats.pt")
        # torch.save(feats,"msmt_trainset_feats.pt")
        # raise KeyboardInterrupt
        if self.feat_norm:
            print("The test feature is normalized")
            feats = torch.nn.functional.normalize(feats, dim=1, p=2)  # along channel


        with open("utils/checks/msmt_research/msmt_trainset_dist_output.txt", "w") as file:
            for i in range(feats.shape[0]):
                # query
                qf = feats[i].unsqueeze(0)
                q_pids = np.asarray([self.pids[i]])
                q_camids = np.asarray([self.camids[i]])
                # gallery
                gf = torch.cat([feats[:i], feats[i+1:]])
                g_pids = np.asarray(self.pids[:i]+self.pids[i+1:])
                g_camids = np.asarray(self.camids[:i]+self.camids[i + 1:])
                if self.reranking:
                    print('=> Enter reranking')
                    # distmat = re_ranking(qf, gf, k1=20, k2=6, lambda_value=0.3)
                    distmat = re_ranking(qf, gf, k1=50, k2=15, lambda_value=0.3)

                else:
                    #print('=> Computing DistMat with euclidean_distance')
                    distmat = euclidean_distance(qf, gf)
                cmc, mAP,top_inds,org_cmc = eval_func_all(distmat, q_pids, g_pids, q_camids, g_camids)
                if top_inds ==0:
                    continue
                # q_ind,suc_fail,rank1_ind,rank2_ind,rank3_ind,rank4_ind,rank5_ind
                error_ind = -1
                line = str(i)+','+str(int(cmc[0]==1))+','+",".join(map(str, top_inds))+"\n"

                #
                file.write(line)
            # logger.info("Validation Results ")
            # logger.info("mAP: {:.1%}".format(mAP))
            # for r in [1, 5, 10]:
            #     logger.info("CMC curve, Rank-{:<3}:{:.1%}".format(r, cmc[r - 1]))
        return cmc, mAP, distmat, self.pids, self.camids, qf, gf

