import argparse
import json
import os
from pathlib import Path
from threading import Thread

import numpy as np
import torch
import yaml
from tqdm import tqdm

from models.experimental import attempt_load
from utils.datasets import create_dataloader
from utils.general import coco80_to_coco91_class, check_dataset, check_file, check_img_size, check_requirements, \
    box_iou, non_max_suppression,non_max_suppression_MA, scale_coords, xyxy2xywh, xywh2xyxy, set_logging, increment_path, colorstr
from utils.metrics import ap_per_class, ConfusionMatrix
from utils.plots import plot_images, output_to_target, plot_study_txt
from utils.torch_utils import select_device, time_synchronized, TracedModel,is_parallel

n_att=4

def test(data,
         weights=None,
         batch_size=32,
         imgsz=640,
         conf_thres=0.001,
         iou_thres=0.6,  # for NMS
         save_json=False,
         single_cls=False,
         augment=False,
         verbose=False,
         model=None,
         dataloader=None,
         save_dir=Path(''),  # for saving images
         save_txt=False,  # for auto-labelling
         save_hybrid=False,  # for hybrid auto-labelling
         save_conf=False,  # save auto-label confidences
         plots=True,
         wandb_logger=None,
         compute_loss=None,
         half_precision=True,
         trace=False,
         is_coco=False,
         v5_metric=False):
    # Initialize/load model and set device
    training = model is not None
    if training:  # called by train.py
        device = next(model.parameters()).device  # get model device

    else:  # called directly
        set_logging()
        device = select_device(opt.device, batch_size=batch_size)

        # Directories
        save_dir = Path(increment_path(Path(opt.project) / opt.name, exist_ok=opt.exist_ok))  # increment run
        (save_dir / 'labels' if save_txt else save_dir).mkdir(parents=True, exist_ok=True)  # make dir

        # Load model
        model = attempt_load(weights, map_location=device)  # load FP32 model
        gs = max(int(model.stride[0].max()), 32)  # grid size (max stride)
        imgsz = check_img_size(imgsz, s=gs)  # check img_size
        
        if trace:
            model = TracedModel(model, device, imgsz)

    # Half
    half = device.type != 'cpu' and half_precision  # half precision only supported on CUDA
    if half:
        model.half()

    # Configure
    model.eval()
    #model.train()
    if isinstance(data, str):
        is_coco = data.endswith('coco.yaml')
        with open(data,encoding='UTF-8') as f:
            data = yaml.load(f, Loader=yaml.SafeLoader)
    check_dataset(data)  # check
    
    iouv = torch.linspace(0.5, 0.95, 10).to(device)  # iou vector for mAP@0.5:0.95
    niou = iouv.numel()

    # Logging
    log_imgs = 0
    if wandb_logger and wandb_logger.wandb:
        log_imgs = min(wandb_logger.log_imgs, 100)
    # Dataloader
    if not training:
        if device.type != 'cpu':
            model(torch.zeros(1, 3, imgsz, imgsz).to(device).type_as(next(model.parameters())))  # run once
        task = opt.task if opt.task in ('train', 'val', 'test') else 'val'  # path to train/val/test images
        dataloader = create_dataloader(data[task], imgsz, batch_size, gs, opt, pad=0.5, rect=True,
                                       prefix=colorstr(f'{task}: '))[0]

    if v5_metric:
        print("Testing with YOLOv5 AP metric...")
    
    seen = 0
    confusion_matrix = [ConfusionMatrix(nc=model.n_classes_lis[k]) for k in range(n_att)]
    names_classes_lis=[]
    for kk in range(n_att):
        names_classes_lis.append({k: v for k, v in enumerate(model.module.names_classes_lis[kk] if is_parallel(model) else model.names_classes_lis[kk])})
    coco91class = coco80_to_coco91_class()
    s = ('%20s' + '%12s' * 6) % ('Class', 'Images', 'Labels', 'P', 'R', 'mAP@.5', 'mAP@.5:.95')
    p, r, f1, mp, mr, map50, map, t0, t1 = 0., 0., 0., [0. for i in range(n_att)], [0. for i in range(n_att)], [0. for i in range(n_att)], [0. for i in range(n_att)], 0., 0.
    loss = torch.zeros(2+n_att, device=device)
    loss_att=torch.zeros((n_att,3),device=device)
    jdict, stats, ap, ap_class, wandb_images = [], [[] for i in range(n_att)], [[] for i in range(n_att)], [[] for i in range(n_att)], []
    for batch_i, (img, targets, paths, shapes) in enumerate(tqdm(dataloader, desc=s)):
        img = img.to(device, non_blocking=True)
        img = img.half() if half else img.float()  # uint8 to fp16/32
        img /= 255.0  # 0 - 255 to 0.0 - 1.0
        targets = targets.to(device)
        nb, _, height, width = img.shape  # batch size, channels, height, width

        with torch.no_grad():
            # Run model
            t = time_synchronized()
            '''
            out,train_out=[],[]
            for k in range(n_att):
                each_out,each_train_out = model(img, augment=augment)[k]  # inference and training outputs
                out.append(each_out)
                train_out.append(each_train_out)
                #print(len(out[i]),out[i][0].size())
                #print(len(train_out[i]),train_out[i][0].size())
            '''
            out_total= model(img, augment=augment)  # inference and training outputs
            out,train_out=[],[]
            for k in range(n_att):
                each_out,each_train_out = out_total[k]  # inference and training outputs
                out.append(each_out)
                train_out.append(each_train_out)

            t0 += time_synchronized() - t

            
            # Compute loss
            if compute_loss:
                pred=[]
                for k in range(n_att):
                    pred.append([x.float() for x in train_out[k]])
                loss += compute_loss(pred, targets)[1][:2+n_att]  # box, obj, cls

            # Run NMS
            targets[:, 1+n_att:] *= torch.Tensor([width, height, width, height]).to(device)  # to pixels
            lb = [targets[targets[:, 0] == i, 1:] for i in range(nb)] if save_hybrid else []  # for autolabelling
            t = time_synchronized()
            """
            out_wei=out[0]
            for k in range(1,n_att):
                out_wei=torch.cat([out_wei,out[k][...,5:]],-1)
            """
            #print(len(out),out[0].size(),out[0])

            out = non_max_suppression_MA(out, conf_thres=conf_thres, iou_thres=iou_thres, labels=lb, multi_label=True)
            #print(len(out),out[0].size())
            t1 += time_synchronized() - t
        
            
        # Statistics per image
        for si, pred in enumerate(out):
            labels = targets[targets[:, 0] == si, 1:]
            nl = len(labels)
            tcls = labels[:, 0:n_att].tolist() if nl else []  # target class
            #print("\ntcls",len(tcls),len(tcls[0]))
            path = Path(paths[si])
            seen += 1

            if len(pred) == 0:
                if nl:
                    for k in range(n_att):
                        stats[k].append((torch.zeros(0, niou, dtype=torch.bool), torch.Tensor(), torch.Tensor(), [x[k] for x in tcls]))
                continue
            
            #print("\npred",len(pred),pred[0])
            # Predictions
            predn = pred.clone()
            scale_coords(img[si].shape[1:], predn[:, :4], shapes[si][0], shapes[si][1])  # native-space pred

            # Append to text file
            if save_txt:
                gn = torch.tensor(shapes[si][0])[[1, 0, 1, 0]]  # normalization gain whwh
                for x1,y1,x2,y2, conf, *cls in predn.tolist():
                    xyxy=[x1,y1,x2,y2]
                    xywh = (xyxy2xywh(torch.tensor(xyxy).view(1, 4)) / gn).view(-1).tolist()  # normalized xywh
                    line = (*cls, *xywh, conf) if save_conf else (*cls, *xywh)  # label format
                    with open(save_dir / 'labels' / (path.stem + '.txt'), 'a') as f:
                        f.write(('%g ' * len(line)).rstrip() % line + '\n')

            # W&B logging - Media Panel Plots
            if len(wandb_images) < log_imgs and wandb_logger.current_epoch > 0:  # Check for test operation
                if wandb_logger.current_epoch % wandb_logger.bbox_interval == 0:
                    for k in range(n_att):            
                        box_data = [{"position": {"minX": x1, "minY": y1, "maxX": x2, "maxY": y2},
                                    "class_id": int(cls[k]),
                                    "box_caption": "%s %.3f" % (names_classes_lis[k][int(cls[k])], conf),
                                    "scores": {"class_score": conf},
                                    "domain": "pixel"} for x1,y1,x2,y2, conf, *cls in pred.tolist()]
                        boxes = {"predictions": {"box_data": box_data, "class_labels":names_classes_lis[k]}}  # inference-space
                        wandb_images.append(wandb_logger.wandb.Image(img[si], boxes=boxes, caption=path.name))

            wandb_logger.log_training_progress(predn, path, names_classes_lis) if wandb_logger and wandb_logger.wandb_run else None

            # Append to pycocotools JSON dictionary
            if save_json:
                # [{"image_id": 42, "category_id": 18, "bbox": [258.15, 41.29, 348.26, 243.78], "score": 0.236}, ...
                image_id = int(path.stem) if path.stem.isnumeric() else path.stem
                box = xyxy2xywh(predn[:, :4])  # xywh
                box[:, :2] -= box[:, 2:] / 2  # xy center to top-left corner
                for p, b in zip(pred.tolist(), box.tolist()):
                    jdict.append({'image_id': image_id,
                                  'category_id': coco91class[int(p[5:5+n_att])] if is_coco else int(p[5:5+n_att]),
                                  'bbox': [round(x, 3) for x in b],
                                  'score': round(p[4], 5)})

            # Assign all predictions as incorrect
            correct = torch.zeros(pred.shape[0], niou, dtype=torch.bool, device=device)
            if nl:
                detected = []  # target indices
                tcls_tensor = labels[:, 0]

                # target boxes
                tbox = xywh2xyxy(labels[:, n_att:4+n_att])
                scale_coords(img[si].shape[1:], tbox, shapes[si][0], shapes[si][1])  # native-space labels
                if plots:
                    for k in range(n_att):
                        confusion_pred=torch.cat((pred[:,:5],pred[:,5+k:6+k]),-1)
                        confusion_label=torch.cat((labels[:,k:k+1],tbox),-1)
                        confusion_matrix[k].process_batch(confusion_pred, confusion_label)

                # Per target class
                for cls in torch.unique(tcls_tensor):
                    ti = (cls == tcls_tensor).nonzero(as_tuple=False).contiguous().view(-1)  # prediction indices
                    pi = (cls == pred[:, 5]).nonzero(as_tuple=False).contiguous().view(-1)  # target indices

                    # Search for detections
                    if pi.shape[0]:
                        # Prediction to target ious
                        ious, i = box_iou(predn[pi, :4], tbox[ti]).max(1)  # best ious, indices

                        # Append detections
                        detected_set = set()
                        for j in (ious > iouv[0]).nonzero(as_tuple=False):
                            d = ti[i[j]]  # detected target
                            if d.item() not in detected_set:
                                detected_set.add(d.item())
                                detected.append(d)
                                correct[pi[j]] = ious[j] > iouv  # iou_thres is 1xn
                                if len(detected) == nl:  # all targets already located in image
                                    break

            # Append statistics (correct, conf, pcls, tcls)
            for k in range(n_att):
                stats[k].append((correct.cpu(), pred[:, 4].cpu(), pred[:, 5+k].cpu(), [x[k] for x in tcls]))

        # Plot images
        if plots and batch_i < 3:
            f = save_dir / f'test_batch{batch_i}_labels.jpg'  # 
            Thread(target=plot_images, args=(img, targets, paths, f, names_classes_lis), daemon=True).start()
            f = save_dir / f'test_batch{batch_i}_pred.jpg'  # predictions
            Thread(target=plot_images, args=(img, output_to_target(out), paths, f, names_classes_lis), daemon=True).start()

    # Compute statistics
    #print("\nstats",len(stats),len(stats[0]),(batch_i+1)*(si+1),stats[0][0])
    for k in range(n_att):
        #print("\nstats",len(stats),len(stats[0]),stats[0])
        each_stats = [np.concatenate(x, 0) for x in zip(*(stats[k]))]  # to numpy
        if len(each_stats) and each_stats[0].any():
            #print("\neach_status true")
            p, r, ap[k], f1, ap_class[k] = ap_per_class(*each_stats, plot=plots, v5_metric=v5_metric, save_dir=save_dir, names=names_classes_lis[k])
            ap50, ap[k] = ap[k][:, 0], ap[k].mean(1)  # AP@0.5, AP@0.5:0.95
            mp[k], mr[k], map50[k], map[k] = p.mean(), r.mean(), ap50.mean(), ap[k].mean()
            nt = np.bincount(each_stats[3].astype(np.int64), minlength=model.n_classes_lis[k])  # number of targets per class
        else:
            #print("\neach_status false")
            nt = torch.zeros(1)
        #print(k,len(ap_class),len(ap_class[0]),ap_class[0])
        # Print results
        pf = '%20s' + '%12i' * 2 + '%12.3g' * 4  # print format
        print(pf % ('all', seen, nt.sum(), mp[k], mr[k], map50[k], map[k]))

        # Print results per class
        if (verbose or (model.n_classes_lis[k] < 50 and not training)) and model.n_classes_lis[k] > 1 and len(each_stats):
            for i, c in enumerate(ap_class[k]):
                print(pf % (names_classes_lis[k][c], seen, nt[c], p[i], r[i], ap50[i], ap[k][i]))

    # Print speeds
    t = tuple(x / seen * 1E3 for x in (t0, t1, t0 + t1)) + (imgsz, imgsz, batch_size)  # tuple
    if not training:
        print('Speed: %.1f/%.1f/%.1f ms inference/NMS/total per %gx%g image at batch-size %g' % t)

    # Plots
    if plots:
        for k in range(n_att):
            confusion_matrix[k].plot(save_dir=save_dir, names=list(names_classes_lis[k].values()))
            if wandb_logger and wandb_logger.wandb:
                val_batches = [wandb_logger.wandb.Image(str(f), caption=f.name) for f in sorted(save_dir.glob('test*.jpg'))]
                wandb_logger.log({"Validation": val_batches})
    if wandb_images:
        wandb_logger.log({"Bounding Box Debugger/Images": wandb_images})

    # Save JSON
    if save_json and len(jdict):
        w = Path(weights[0] if isinstance(weights, list) else weights).stem if weights is not None else ''  # weights
        anno_json = './datasets/coco/annotations/instances_val2017.json'  # annotations json
        pred_json = str(save_dir / f"{w}_predictions.json")  # predictions json
        print('\nEvaluating pycocotools mAP... saving %s...' % pred_json)
        with open(pred_json, 'w') as f:
            json.dump(jdict, f)

        try:  # https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocoEvalDemo.ipynb
            from pycocotools.coco import COCO
            from pycocotools.cocoeval import COCOeval

            anno = COCO(anno_json)  # init annotations api
            pred = anno.loadRes(pred_json)  # init predictions api
            eval = COCOeval(anno, pred, 'bbox')
            if is_coco:
                eval.params.imgIds = [int(Path(x).stem) for x in dataloader.dataset.img_files]  # image IDs to evaluate
            eval.evaluate()
            eval.accumulate()
            eval.summarize()
            map[0], map50[0] = eval.stats[:2]  # update results (mAP@0.5:0.95, mAP@0.5)
        except Exception as e:
            print(f'pycocotools unable to run: {e}')

    # Return results
    model.float()  # for training
    if not training:
        s = f"\n{len(list(save_dir.glob('labels/*.txt')))} labels saved to {save_dir / 'labels'}" if save_txt else ''
        print(f"Results saved to {save_dir}{s}")
    
    maps=[]
    for k in range(n_att):
        maps.append(np.zeros(model.n_classes_lis[k]) + map[k])
        #print(len(ap_class),len(ap_class[0]))
        for i, c in enumerate(ap_class[k]):
            maps[k][c] = ap[k][i]

        loss_att[k,0:2],loss_att[k,2]=loss[0:2],loss[2+k]
    #print("\nloss_att",loss_att.size())
    return [(mp[k], mr[k], map50[k], map[k], *(loss_att[k].cpu() / len(dataloader)).tolist()) for k in range(n_att)], maps, t


if __name__ == '__main__':
    parser = argparse.ArgumentParser(prog='test.py')
    parser.add_argument('--weights', nargs='+', type=str, default='yolov7.pt', help='model.pt path(s)')
    parser.add_argument('--data', type=str, default='data/coco.yaml', help='*.data path')
    parser.add_argument('--batch-size', type=int, default=32, help='size of each image batch')
    parser.add_argument('--img-size', type=int, default=640, help='inference size (pixels)')
    parser.add_argument('--conf-thres', type=float, default=0.001, help='object confidence threshold')
    parser.add_argument('--iou-thres', type=float, default=0.65, help='IOU threshold for NMS')
    parser.add_argument('--task', default='val', help='train, val, test, speed or study')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--single-cls', action='store_true', help='treat as single-class dataset')
    parser.add_argument('--augment', action='store_true', help='augmented inference')
    parser.add_argument('--verbose', action='store_true', help='report mAP by class')
    parser.add_argument('--save-txt', action='store_true', help='save results to *.txt')
    parser.add_argument('--save-hybrid', action='store_true', help='save label+prediction hybrid results to *.txt')
    parser.add_argument('--save-conf', action='store_true', help='save confidences in --save-txt labels')
    parser.add_argument('--save-json', action='store_true', help='save a cocoapi-compatible JSON results file')
    parser.add_argument('--project', default='runs/test', help='save to project/name')
    parser.add_argument('--name', default='exp', help='save to project/name')
    parser.add_argument('--exist-ok', action='store_true', help='existing project/name ok, do not increment')
    parser.add_argument('--no-trace', action='store_true', help='don`t trace model')
    parser.add_argument('--v5-metric', action='store_true', help='assume maximum recall as 1.0 in AP calculation')
    opt = parser.parse_args()
    opt.save_json |= opt.data.endswith('coco.yaml')
    opt.data = check_file(opt.data)  # check file
    print(opt)
    #check_requirements()

    if opt.task in ('train', 'val', 'test'):  # run normally
        test(opt.data,
             opt.weights,
             opt.batch_size,
             opt.img_size,
             opt.conf_thres,
             opt.iou_thres,
             opt.save_json,
             opt.single_cls,
             opt.augment,
             opt.verbose,
             save_txt=opt.save_txt | opt.save_hybrid,
             save_hybrid=opt.save_hybrid,
             save_conf=opt.save_conf,
             trace=not opt.no_trace,
             v5_metric=opt.v5_metric
             )

    elif opt.task == 'speed':  # speed benchmarks
        for w in opt.weights:
            test(opt.data, w, opt.batch_size, opt.img_size, 0.25, 0.45, save_json=False, plots=False, v5_metric=opt.v5_metric)

    elif opt.task == 'study':  # run over a range of settings and save/plot
        # python test.py --task study --data coco.yaml --iou 0.65 --weights yolov7.pt
        x = list(range(256, 1536 + 128, 128))  # x axis (image sizes)
        for w in opt.weights:
            f = f'study_{Path(opt.data).stem}_{Path(w).stem}.txt'  # filename to save to
            y = []  # y axis
            for i in x:  # img-size
                print(f'\nRunning {f} point {i}...')
                r, _, t = test(opt.data, w, opt.batch_size, i, opt.conf_thres, opt.iou_thres, opt.save_json,
                               plots=False, v5_metric=opt.v5_metric)
                y.append(r + t)  # results and times
            np.savetxt(f, y, fmt='%10.4g')  # save
        os.system('zip -r study.zip study_*.txt')
        plot_study_txt(x=x)  # plot
