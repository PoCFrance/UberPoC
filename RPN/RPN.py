import os
import glob
import numpy as np
import numpy.random as npr
from keras.models import Input, Model
from keras.layers import Conv2D
from keras.applications import InceptionResNetV2
from keras.preprocessing.image import load_img, img_to_array
from utils import generate_anchors, draw_anchors, bbox_overlaps, bbox_transform,\
loss_cls, smoothL1, parse_label, unmap
from PIL import Image


class InputGenerator:

    def __init__(self):
        self.pretrained_model = InceptionResNetV2(include_top=False)
        img = load_img("./ILSVRC2014_train_00010391.jpeg")
        x = img_to_array(img)
        x = np.expand_dims(x, axis=0)
        not_used = self.pretrained_model.predict(x)

        self.k_anch = 9
        self.BG_FG_FRAC = 2
        self.BATCH_SIZE = 512

    def generate(self, dataset_path=""):
        import glob
        batch_tiles = []
        batch_labels = []
        batch_bboxes = []
        while 1:
            for fname in glob.glob(f"{dataset_path}/*.xml"):
                try:
                    category, gt_boxes, scale = parse_label(fname)
                    if len(gt_boxes) == 0:
                        continue
                    basename = fname.split('.xml')[0]
                    tiles, labels, bboxes = self.create_batch(f"{basename}.jpg", gt_boxes, scale)
                except Exception:
                    print(f"Couldn't parse label or produce batch for : {fname}")
                    continue
                for i in range(len(tiles)):
                    batch_tiles.append(tiles[i])
                    batch_labels.append(labels[i])
                    batch_bboxes.append(bboxes[i])
                    a = np.asarray(batch_tiles)
                    if len(batch_tiles) == self.BATCH_SIZE:
                        b = np.asarray(batch_labels)
                        c = np.asarray(batch_bboxes)
                        if not a.any() or not b.any() or c.any():
                            print("empty array found.")
                            continue
                        yield a, [b, c]
                        batch_labels = []
                        batch_bboxes = []
                        batch_tiles = []


    def normalize_img(self, filepath, scale):
        img = load_img(filepath)
        img_width = np.shape(img)[1] * scale[1]
        img_height = np.shape(img)[0] * scale[0]
        img = img.resize((int(img_width), int(img_height)))
        # feed image to pretrained model and get feature map
        img = img_to_array(img)
        img = np.expand_dims(img, axis=0)
        return img_width, img_height, img

    def produce_anchors(self, num_feature_map, w_stride, h_stride, width, height, img_width, img_height):
        base_anchors = generate_anchors(w_stride, h_stride)
        # slice tiles according to image size and stride.
        # each 1x1x1532 feature map is mapping to a tile.
        shift_x = np.arange(0, width) * w_stride
        shift_y = np.arange(0, height) * h_stride
        shift_x, shift_y = np.meshgrid(shift_x, shift_y)
        shifts = np.vstack((shift_x.ravel(), shift_y.ravel(), shift_x.ravel(),
                            shift_y.ravel())).transpose()
        # apply base anchors to all tiles, to have a num_feature_map*9 anchors.
        all_anchors = (base_anchors.reshape((1, 9, 4)) +
                       shifts.reshape((1, num_feature_map, 4)).transpose((1, 0, 2)))
        total_anchors = num_feature_map * 9
        all_anchors = all_anchors.reshape((total_anchors, 4))
        # only keep anchors inside image+borader.
        border = 0
        inds_inside = np.where(
            (all_anchors[:, 0] >= -border) &
            (all_anchors[:, 1] >= -border) &
            (all_anchors[:, 2] < img_width + border) &  # width
            (all_anchors[:, 3] < img_height + border)  # height
        )[0]
        anchors = all_anchors[inds_inside]
        return total_anchors, all_anchors, inds_inside, anchors



    def create_batch(self, filepath, gt_boxes, scale):
        img_width, img_height, img = self.normalize_img(filepath, scale)

        feature_map = self.pretrained_model.predict(img)
        height = np.shape(feature_map)[1]
        width = np.shape(feature_map)[2]
        num_feature_map = width * height
        # calculate output w, h stride
        w_stride = img_width / width
        h_stride = img_height / height
        # generate base anchors according output stride.
        # base anchors are 9 anchors wrt a tile (0,0,w_stride-1,h_stride-1)
        total_anchors, all_anchors, inds_inside, anchors = self.produce_anchors(
            num_feature_map,
            w_stride,
            h_stride,
            width,
            height,
            img_width,
            img_height
        )
        # calculate overlaps each anchors to each gt boxes,
        # a matrix with shape [len(anchors) x len(gt_boxes)]
        overlaps = bbox_overlaps(anchors, gt_boxes)
        # find the gt box with biggest overlap to each anchors,
        # and the overlap ratio. result (len(anchors),)
        argmax_overlaps = overlaps.argmax(axis=1)
        max_overlaps = overlaps[np.arange(len(inds_inside)), argmax_overlaps]
        # find the anchor with biggest overlap to each gt boxes,
        # and the overlap ratio. result (len(gt_boxes),)
        gt_argmax_overlaps = overlaps.argmax(axis=0)
        gt_max_overlaps = overlaps[gt_argmax_overlaps,
                                   np.arange(overlaps.shape[1])]
        gt_argmax_overlaps = np.where(overlaps == gt_max_overlaps)[0]
        # labels, 1=fg/0=bg/-1=ignore
        labels = np.empty((len(inds_inside),), dtype=np.float32)
        labels.fill(-1)
        # set positive label, define in Paper3.1.2:
        # We assign a positive label to two kinds of anchors: (i) the
        # anchor/anchors with the highest Intersection-overUnion
        # (IoU) overlap with a ground-truth box, or (ii) an
        # anchor that has an IoU overlap higher than 0.7 with any gt boxes
        labels[gt_argmax_overlaps] = 1

        labels[max_overlaps >= .7] = 1
        # set negative labels
        labels[max_overlaps <= .3] = 0
        # subsample positive labels if we have too many
        #     num_fg = int(RPN_FG_FRACTION * RPN_BATCHSIZE)
        fg_inds = np.where(labels == 1)[0]

        #     if len(fg_inds) > num_fg:
        #         disable_inds = npr.choice(
        #             fg_inds, size=(len(fg_inds) - num_fg), replace=False)
        #         labels[disable_inds] = -1
        # subsample negative labels if we have too many
        num_bg = int(len(fg_inds) * self.BG_FG_FRAC)
        bg_inds = np.where(labels == 0)[0]

        if len(bg_inds) > num_bg:
            disable_inds = npr.choice(
                bg_inds, size=(len(bg_inds) - num_bg), replace=False)
            labels[disable_inds] = -1
        #
        batch_inds = inds_inside[labels != -1]
        batch_inds = (batch_inds / self.k_anch).astype(np.int)

        full_labels = unmap(labels, total_anchors, inds_inside, fill=-1)
        batch_label_targets = full_labels.reshape(-1, 1, 1, 1 * self.k_anch)[batch_inds]
        bbox_targets = np.zeros((len(inds_inside), 4), dtype=np.float32)
        # bbox_targets = bbox_transform(anchors, gt_boxes[argmax_overlaps, :]
        pos_anchors = all_anchors[inds_inside[labels == 1]]
        bbox_targets = bbox_transform(pos_anchors, gt_boxes[argmax_overlaps, :][labels == 1])
        bbox_targets = unmap(bbox_targets, total_anchors, inds_inside[labels == 1], fill=0)
        batch_bbox_targets = bbox_targets.reshape(-1, 1, 1, 4 * self.k_anch)[batch_inds]
        padded_fcmap = np.pad(feature_map, ((0, 0), (1, 1), (1, 1), (0, 0)), mode='constant')
        padded_fcmap = np.squeeze(padded_fcmap)
        batch_tiles = []
        for ind in batch_inds:
            x = ind % width
            y = int(ind / width)
            fc_3x3 = padded_fcmap[y:y + 3, x:x + 3, :]
            batch_tiles.append(fc_3x3)
        return np.asarray(batch_tiles), batch_label_targets.tolist(), batch_bbox_targets.tolist()



class RPN:
    def __init__(self):
        # Hyper parameters of our model
        self.k_anch = 9
        # extra configuration

        feature_map_tile = Input(shape=(None, None, 1536))
        conv3x3 = Conv2D(
            filters=512,
            kernel_size=(3,3),
            padding='same',
            name="3x3"
        )(feature_map_tile)
        output_deltas = Conv2D(
            filters= 4 * self.k_anch,
            kernel_size=(1,1),
            activation='linear',
            kernel_initializer="uniform",
            name="deltas1"
        )(conv3x3)
        output_scores = Conv2D(
            filters=1 * self.k_anch,
            kernel_size=(1,1),
            activation="sigmoid",
            kernel_initializer="uniform",
            name="scores1"
        )(conv3x3)
        self.model = Model(inputs=[feature_map_tile], outputs=[output_scores, output_deltas])
        self.model.compile(optimizer='adam', loss={'scores1': loss_cls, 'deltas1': smoothL1})

    def train(self):
        from keras.callbacks import ModelCheckpoint
        checkpointer = ModelCheckpoint(filepath='./weights.hdf5', verbose=1, save_best_only=True)
        generator = InputGenerator()
        self.model.fit(generator.generate("./TRAIN_DATA"), steps_per_epoch=10, epochs=10, callbacks=[checkpointer])


rpn = RPN()
rpn.train()
