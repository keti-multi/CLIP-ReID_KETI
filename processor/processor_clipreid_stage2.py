import logging
import os
import time
import torch
import torch.nn as nn
from utils.meter import AverageMeter
from utils.metrics import R1_mAP_eval
from torch.cuda import amp
import torch.distributed as dist
from torch.nn import functional as F
from loss.supcontrast import SupConLoss
from PIL import Image, ImageFile
import numpy as np
import cv2

import matplotlib.pyplot as plt


from pytorch_grad_cam import GradCAM, \
    ScoreCAM, \
    GradCAMPlusPlus, \
    AblationCAM, \
    XGradCAM, \
    EigenCAM, \
    EigenGradCAM, \
    LayerCAM, \
    FullGrad
from pytorch_grad_cam.utils.image import show_cam_on_image, \
    preprocess_image
from pytorch_grad_cam.ablation_layer import AblationLayerVit

from utils.visualization.baselines.ViT.ViT_explanation_generator import LRP


def generate_visualization(original_image, attribution_generator,class_index=None):
    transformer_attribution = attribution_generator.generate_LRP(original_image.cuda(),
                                                                 method="transformer_attribution",
                                                                 index=class_index).detach()
    transformer_attribution = transformer_attribution.reshape(1, 1, 16, 8)
    transformer_attribution = torch.nn.functional.interpolate(transformer_attribution, scale_factor=16, mode='bilinear')
    transformer_attribution = transformer_attribution.reshape(256, 128).data.cpu().numpy()
    transformer_attribution = (transformer_attribution - transformer_attribution.min()) / (
                transformer_attribution.max() - transformer_attribution.min())

    if use_thresholding:
        transformer_attribution = transformer_attribution * 255
        transformer_attribution = transformer_attribution.astype(np.uint8)
        ret, transformer_attribution = cv2.threshold(transformer_attribution, 0, 255,
                                                     cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        transformer_attribution[transformer_attribution == 255] = 1

    image_transformer_attribution = original_image.permute(1, 2, 0).data.cpu().numpy()
    image_transformer_attribution = (image_transformer_attribution - image_transformer_attribution.min()) / (
                image_transformer_attribution.max() - image_transformer_attribution.min())
    vis = show_cam_on_image(image_transformer_attribution, transformer_attribution)
    vis = np.uint8(255 * vis)
    vis = cv2.cvtColor(np.array(vis), cv2.COLOR_RGB2BGR)
    return vis


def print_top_classes(predictions, **kwargs):
    # Print Top-5 predictions
    prob = torch.softmax(predictions, dim=1)
    class_indices = predictions.data.topk(5, dim=1)[1][0].tolist()
    max_str_len = 0
    class_names = []
    for cls_idx in class_indices:
        class_names.append(CLS2IDX[cls_idx])
        if len(CLS2IDX[cls_idx]) > max_str_len:
            max_str_len = len(CLS2IDX[cls_idx])

    print('Top 5 classes:')
    for cls_idx in class_indices:
        output_string = '\t{} : {}'.format(cls_idx, CLS2IDX[cls_idx])
        output_string += ' ' * (max_str_len - len(CLS2IDX[cls_idx])) + '\t\t'
        output_string += 'value = {:.3f}\t prob = {:.1f}%'.format(predictions[0, cls_idx], 100 * prob[0, cls_idx])
        print(output_string)
#
# def reshape_transform(tensor, height=14, width=14):
#     result = tensor[:, 1:, :].reshape(tensor.size(0),
#                                       height, width, tensor.size(2))
#
#     # Bring the channels to the first dimension,
#     # like in CNNs.
#     result = result.transpose(2, 3).transpose(1, 2)
#     return result

def reshape_transform(tensor, height=16, width=8):
    # print(tensor.max())
    # print(tensor.shape)# 129 64 768
    result = tensor[1:, :, :].reshape(tensor.size(1),
                                      height, width, tensor.size(2))
    # result = tensor[:, 1:, :].reshape(tensor.size(0),
    #                                   width, height, tensor.size(2))

    # 64 * 8 * 16 * 768
    # Bring the channels to the first dimension,
    # like in CNNs.
    # result = result.transpose(2, 3).transpose(1, 2).transpose(2,3)
    result = result.transpose(2, 3).transpose(1, 2)#.transpose(2,3)
    # 64 * 768 * 8 * 16

    return result

def do_train_stage2(cfg,
             model,
             center_criterion,
             train_loader_stage2,
             val_loader,
             optimizer,
             optimizer_center,
             scheduler,
             loss_fn,
             num_query, local_rank):
    log_period = cfg.SOLVER.STAGE2.LOG_PERIOD
    checkpoint_period = cfg.SOLVER.STAGE2.CHECKPOINT_PERIOD
    eval_period = cfg.SOLVER.STAGE2.EVAL_PERIOD
    instance = cfg.DATALOADER.NUM_INSTANCE

    device = "cuda"
    epochs = cfg.SOLVER.STAGE2.MAX_EPOCHS

    logger = logging.getLogger("transreid.train")
    logger.info('start training')
    _LOCAL_PROCESS_GROUP = None
    if device:
        model.to(local_rank)
        if torch.cuda.device_count() > 1:
            print('Using {} GPUs for training'.format(torch.cuda.device_count()))
            model = nn.DataParallel(model)  
            num_classes = model.module.num_classes
        else:
            num_classes = model.num_classes

    loss_meter = AverageMeter()
    acc_meter = AverageMeter()

    evaluator = R1_mAP_eval(num_query, max_rank=50, feat_norm=cfg.TEST.FEAT_NORM)
    scaler = amp.GradScaler()
    xent = SupConLoss(device)
    
    # train
    import time
    from datetime import timedelta
    all_start_time = time.monotonic()

    # train
    batch = cfg.SOLVER.STAGE2.IMS_PER_BATCH
    i_ter = num_classes // batch
    left = num_classes-batch* (num_classes//batch)
    if left != 0 :
        i_ter = i_ter+1
    text_features = []
    text_features_cluster= []
    with torch.no_grad():
        if cfg.DATASETS.CLUSTER:
            cluster_trig=torch.ones(64)
            for i in range(i_ter):
                if i+1 != i_ter:
                    l_list = torch.arange(i*batch, (i+1)* batch)
                else:
                    l_list = torch.arange(i*batch, num_classes)
                with amp.autocast(enabled=True):
                    text_feature = model(label=l_list, get_text=True)
                    text_feature_cluster = model.forward_clustered(label=l_list, get_text=True, cluster=cluster_trig)
                text_features.append(text_feature.cpu())
                text_features_cluster.append(text_feature_cluster.cpu())
            text_features = torch.cat(text_features, 0).cuda()
            text_features_cluster = torch.cat(text_features_cluster, 0).cuda()
        else:
            for i in range(i_ter):
                if i+1 != i_ter:
                    l_list = torch.arange(i*batch, (i+1)* batch)
                else:
                    l_list = torch.arange(i*batch, num_classes)
                with amp.autocast(enabled=True):
                    text_feature = model(label = l_list, get_text = True)
                text_features.append(text_feature.cpu())
            text_features = torch.cat(text_features, 0).cuda()
    for epoch in range(1, epochs + 1):
        start_time = time.time()
        loss_meter.reset()
        acc_meter.reset()
        evaluator.reset()

        scheduler.step()

        model.train()
        for n_iter, items in enumerate(train_loader_stage2):
            if len(items)==5:
                img, vid, target_cam, target_view,clusters = items
            else:
                img, vid, target_cam, target_view=items

            optimizer.zero_grad()
            optimizer_center.zero_grad()
            img = img.to(device)
            target = vid.to(device)
            if cfg.MODEL.SIE_CAMERA:
                target_cam = target_cam.to(device)
            else:
                target_cam = None
            if cfg.MODEL.SIE_VIEW:
                target_view = target_view.to(device)
            else:
                target_view = None
            with amp.autocast(enabled=True):
                score, feat, image_features = model(x = img, label = target, cam_label=target_cam, view_label=target_view)
                if len(items)==5:
                    ## Todo 230926 make max pooling in solo-clustered
                    text_features_ = torch.cat((text_features, text_features_cluster), dim=0)
                    logits = image_features @ text_features_.t()
                    first_part = logits[:, :num_classes]
                    second_part = logits[:, num_classes:]
                    # Compare and select the max values
                    logits = torch.max(first_part, second_part)

                else:
                    logits = image_features @ text_features.t()
                loss = loss_fn(score, feat, target, target_cam, logits)

            scaler.scale(loss).backward()

            scaler.step(optimizer)
            scaler.update()

            if 'center' in cfg.MODEL.METRIC_LOSS_TYPE:
                for param in center_criterion.parameters():
                    param.grad.data *= (1. / cfg.SOLVER.CENTER_LOSS_WEIGHT)
                scaler.step(optimizer_center)
                scaler.update()

            acc = (logits.max(1)[1] == target).float().mean()

            loss_meter.update(loss.item(), img.shape[0])
            acc_meter.update(acc, 1)

            torch.cuda.synchronize()
            if (n_iter + 1) % log_period == 0:
                logger.info("Epoch[{}] Iteration[{}/{}] Loss: {:.3f}, Acc: {:.3f}, Base Lr: {:.2e}"
                            .format(epoch, (n_iter + 1), len(train_loader_stage2),
                                    loss_meter.avg, acc_meter.avg, scheduler.get_lr()[0]))

        end_time = time.time()
        time_per_batch = (end_time - start_time) / (n_iter + 1)
        if cfg.MODEL.DIST_TRAIN:
            pass
        else:
            logger.info("Epoch {} done. Time per batch: {:.3f}[s] Speed: {:.1f}[samples/s]"
                    .format(epoch, time_per_batch, train_loader_stage2.batch_size / time_per_batch))

        if epoch % checkpoint_period == 0:
            if cfg.MODEL.DIST_TRAIN:
                if dist.get_rank() == 0:
                    torch.save(model.state_dict(),
                               os.path.join(cfg.OUTPUT_DIR, cfg.MODEL.NAME + '_{}.pth'.format(epoch)))
            else:
                torch.save(model.state_dict(),
                           os.path.join(cfg.OUTPUT_DIR, cfg.MODEL.NAME + '_{}.pth'.format(epoch)))

        if epoch % eval_period == 0:
            if cfg.MODEL.DIST_TRAIN:
                if dist.get_rank() == 0:
                    model.eval()
                    for n_iter, (img, vid, camid, camids, target_view, _) in enumerate(val_loader):
                        with torch.no_grad():
                            img = img.to(device)
                            if cfg.MODEL.SIE_CAMERA:
                                camids = camids.to(device)
                            else: 
                                camids = None
                            if cfg.MODEL.SIE_VIEW:
                                target_view = target_view.to(device)
                            else: 
                                target_view = None
                            feat = model(img, cam_label=camids, view_label=target_view)
                            evaluator.update((feat, vid, camid))
                    cmc, mAP, _, _, _, _, _ = evaluator.compute()
                    logger.info("Validation Results - Epoch: {}".format(epoch))
                    logger.info("mAP: {:.1%}".format(mAP))
                    for r in [1, 5, 10]:
                        logger.info("CMC curve, Rank-{:<3}:{:.1%}".format(r, cmc[r - 1]))
                    torch.cuda.empty_cache()
            else:
                model.eval()
                for n_iter, (img, vid, camid, camids, target_view, _) in enumerate(val_loader):
                    with torch.no_grad():
                        img = img.to(device)
                        if cfg.MODEL.SIE_CAMERA:
                            camids = camids.to(device)
                        else: 
                            camids = None
                        if cfg.MODEL.SIE_VIEW:
                            target_view = target_view.to(device)
                        else: 
                            target_view = None
                        feat = model(img, cam_label=camids, view_label=target_view)
                        evaluator.update((feat, vid, camid))
                cmc, mAP, _, _, _, _, _ = evaluator.compute()
                logger.info("Validation Results - Epoch: {}".format(epoch))
                logger.info("mAP: {:.1%}".format(mAP))
                for r in [1, 5, 10]:
                    logger.info("CMC curve, Rank-{:<3}:{:.1%}".format(r, cmc[r - 1]))
                torch.cuda.empty_cache()

    all_end_time = time.monotonic()
    total_time = timedelta(seconds=all_end_time - all_start_time)
    logger.info("Total running time: {}".format(total_time))
    print(cfg.OUTPUT_DIR)

def do_inference(cfg,
                 model,
                 val_loader,
                 num_query):

    GRAD_CAM=False
    TRANS_INTPRET=False

    device = "cuda"
    logger = logging.getLogger("transreid.test")
    logger.info("Enter inferencing")

    evaluator = R1_mAP_eval(num_query, max_rank=50, feat_norm=cfg.TEST.FEAT_NORM)
    evaluator.reset()

    if device:
        if torch.cuda.device_count() > 1:
            print('Using {} GPUs for inference'.format(torch.cuda.device_count()))
            model = nn.DataParallel(model)
        model.to(device)

    model.eval()
    if TRANS_INTPRET:
        attribution_generator = LRP(model)
    if GRAD_CAM:
        gradcam_methods = \
            {"gradcam": GradCAM,
             "scorecam": ScoreCAM,
             "gradcam++": GradCAMPlusPlus,
             "ablationcam": AblationCAM,
             "xgradcam": XGradCAM,
             "eigencam": EigenCAM,
             "eigengradcam": EigenGradCAM,
             "layercam": LayerCAM,
             "fullgrad": FullGrad}

    # target_layers = [model.blocks[-1].norm1]
    # torch.cat([img_feature, img_feature_proj], dim=1)
        target_layers = [model.image_encoder.transformer.resblocks[9].ln_1,model.image_encoder.transformer.resblocks[10].ln_1,model.image_encoder.transformer.resblocks[11].ln_1]
        # target_layers = [model.image_encoder.transformer.resblocks[10].mlp]
        # target_layers = [model.image_encoder.ln_post]
        # target_layers = [model.image_encoder]
        cam = gradcam_methods['gradcam++'](model=model,
                                   target_layers=target_layers,
                                   use_cuda=True,
                                   reshape_transform=reshape_transform)
        cam.batch_size = cfg.TEST.IMS_PER_BATCH


    class EmptyContext:
        def __enter__(self):
            pass
        def __exit__(self, exc_type, exc_value, traceback):
            pass

    from pytorch_grad_cam.utils.model_targets import ClassifierOutputTarget
    text_out_list=[]

    # todo 230921 prompt output save with trainset
    with open('trainset_prompt_output.txt', 'w') as train_prompts:
        for n_iter, (img, pid, camid, camids, target_view, imgpath) in enumerate(val_loader):
            with torch.no_grad():
            # with EmptyContext():
                img = img.to(device)
                if cfg.MODEL.SIE_CAMERA:
                    camids = camids.to(device)
                else:
                    camids = None
                if cfg.MODEL.SIE_VIEW:
                    target_view = target_view.to(device)
                else:
                    target_view = None
                #feat = model(img, cam_label=camids, view_label=target_view)
                # 230921 SYH add text output
                # if WRITE_PROMPT:
                #    _,text_out = model(label=torch.tensor(pid, dtype=torch.int64),get_text=True)
                # train_prompts.writelines([text_[0][:-68]+'\n' for text_ in text_out])

                # print(text_out)
                #feat = model(img, cam_label=camids, view_label=target_view)
                # todo 230921 make msmt trainset validation
                feat = torch.tensor(0)#model(img, cam_label=camids, view_label=target_view)

                if TRANS_INTPRET:
                    cat = generate_visualization(img,attribution_generator)
                    fig, axs = plt.subplots(1, 2)
                    axs[0].imshow(img)
                    axs[0].axis('off')

                    axs[1].imshow(cat)
                    axs[1].axis('off')

                evaluator.update((feat, pid, camid))

                # img_path_list.extend(imgpath)
                if GRAD_CAM:
                    targets_cam =None
                    # if cfg.DATASETS.NAMES == 'msmt17':
                    #     targets_cam = [ClassifierOutputTarget(int(imgpath[i][:4])) for i in range(cfg.TEST.IMS_PER_BATCH)]
                    # else:
                    #     targets_cam = [ClassifierOutputTarget(tar_) for tar_ in
                    #                    torch.argmax(model.classifier(feat[:, :768]), dim=1)]
                    grayscale_cam = cam(input_tensor=img,
                                        targets=targets_cam,
                                        eigen_smooth=False,
                                        aug_smooth=False)
                    for i in range(len(imgpath)):
                        grayscale_cam_ = grayscale_cam[i, :]
                        # rgb_img = Image.open().convert('RGB')
                        if cfg.DATASETS.NAMES=='msmt17':
                            rgb_img = cv2.imread(os.path.join("/media/syh/ssd2/data/ReID/MSMT17/test",imgpath[i][:4],imgpath[i]), 1)[:, :,::-1]
                        else:
                            rgb_img = cv2.imread(os.path.join("/media/syh/ssd2/data/ReID/MUF_KETI/bounding_box_train",imgpath[i]), 1)[:, :, ::-1]
                        rgb_img = cv2.resize(rgb_img, (128, 256))
                        rgb_img = np.float32(rgb_img) / 255

                        cam_image = show_cam_on_image(rgb_img, grayscale_cam_)
                        save_name = imgpath[i].split('.')[0]+'_grad'+'.jpg'
                        cv2.imwrite(os.path.join('/media/syh/ssd2/data/ReID/MSMT17/query_ClipReID_output_grad',save_name), cam_image)

        # todo 230921 make msmt trainset validation
        # cmc, mAP, _, _, _, _, _ = evaluator.compute()
        cmc, mAP, _, _, _, _, _ = evaluator.compute_train_all(logger)
        logger.info("Validation Results ")
        logger.info("mAP: {:.1%}".format(mAP))
        for r in [1, 5, 10]:
            logger.info("CMC curve, Rank-{:<3}:{:.1%}".format(r, cmc[r - 1]))
        return cmc[0], cmc[4]