import torch
import os
import tqdm
from data_preparation import data_utils, image_labelling
from .metrics import ObjectDetectionMetrics
from abc import ABC, abstractmethod
from torchvision.io import read_image


class Evaluator(ABC):
    def __init__(self, model, test_imgs, test_labels, device, class_dict, save_dir):
        self.model = model
        self.test_imgs = test_imgs  # path to the directory holding the test/valid set images
        self.test_labels = test_labels  # path to the directory holding the test/valid set labels
        self.device = device
        self.class_dict = class_dict
        self.num_classes = len(class_dict.keys())
        self.save_dir = save_dir

        self.preds = []  # list of tensors, each tensor holding the predictions for one image
        self.gt = []  # list of tensors, each tensor holding the ground truth labels for one image

        self.__get_preds_and_labels()

        self.metrics = ObjectDetectionMetrics(save_dir=self.save_dir,
                                              idx_to_name=self.class_dict,
                                              detections=self.preds,
                                              ground_truths=self.gt,
                                              device=self.device)

    @abstractmethod
    def infer_for_one_img(self, img_path):
        """ Get predictions along with ground truth labels for a given image path. """
        pass

    def get_labels_for_image(self, img_path):
        filename = data_utils.get_filename(img_path)
        label_path = os.path.join(self.test_labels, filename + ".txt")

        if not os.path.exists(label_path):
            raise Exception(f"Label file for {filename} not found in label directory")

        bboxes, labels = image_labelling.bboxes_from_yolo_labels(label_path, normalised=False)
        labels = torch.as_tensor(labels, dtype=torch.int32)  # convert from list to tensor
        labels = labels.unsqueeze(1)

        return torch.cat((bboxes, labels), dim=1)

    def __get_preds_and_labels(self):
        """
        Runs inference on all the images found in `self.test_imgs` and stores predictions + matching labels in
        the relevant instance attributes.

        This should only be run once upon instantiation of the class.
        """
        # clear attributes to prevent double counting images
        self.preds = []
        self.gt = []

        img_paths = data_utils.list_files_of_a_type(self.test_imgs, ".png")

        print("Running inference on test set...")
        for i in tqdm.tqdm(range(len(img_paths))):
            img_path = img_paths[i]
            ground_truths, predictions = self.infer_for_one_img(img_path)

            self.preds.append(predictions)
            self.gt.append(ground_truths)

    def confusion_matrix(self, conf_threshold=0.25, all_iou=False, plot=False):
        if not self.preds and not self.gt:
            raise Exception("No predictions and/or ground truths found")

        return self.metrics.get_confusion_matrix(conf_threshold, all_iou=all_iou, plot=plot)

    def ap_per_class(self, plot=False, plot_all=False, prefix=""):
        """
        ap (tensor[t, nc]): for t iou thresholds, nc classes
        """
        if not self.preds and not self.gt:
            raise Exception("No predictions and/or ground truths found")

        ap = self.metrics.ap_per_class(plot=plot, plot_all=plot_all, prefix=prefix)
        return ap

    def map50(self):
        return self.metrics.get_map50()

    def map50_95(self):
        return self.metrics.get_map50_95()


class YoloEvaluator(Evaluator):
    def __init__(self, model, test_imgs, test_labels, device, class_dict, save_dir):
        super().__init__(model, test_imgs, test_labels, device, class_dict, save_dir)

    def infer_for_one_img(self, img_path):
        ground_truths = self.get_labels_for_image(img_path)  # (N, 5) where N = number of labels
        predictions = self.model(img_path, verbose=False, conf=0, device=self.device)[0].boxes.data

        return ground_truths, predictions


class RCNNEvaluator(Evaluator):
    def __init__(self, model, test_imgs, test_labels, device, class_dict, save_dir):
        super().__init__(model, test_imgs, test_labels, device, class_dict, save_dir)

    def infer_for_one_img(self, img_path):
        from torchvision.transforms import v2 as T

        self.model.eval()

        image = read_image(img_path)

        transforms = []
        transforms.append(T.ToDtype(torch.float, scale=True))
        transforms.append(T.ToPureTensor())
        transforms = T.Compose(transforms)

        with torch.no_grad():
            x = transforms(image)
            # convert RGBA -> RGB and move to device
            x = x[:3, ...].to(self.device)
            predictions = self.model([x, ])
            pred = predictions[0]

        labels = pred['labels']

        i = labels != 0  # indices of non-background class predictions
        bboxes = pred['boxes'][i]
        scores = pred['scores'][i].unsqueeze(-1)
        labels = labels[i].unsqueeze(-1) - 1

        # predictions (n, 6) for n predictions
        predictions = torch.cat([bboxes, scores, labels], dim=-1)

        ground_truths = self.get_labels_for_image(img_path)  # (N, 5) where N = number of labels

        return ground_truths, predictions
