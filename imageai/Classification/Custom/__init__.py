import time, warnings
import os
import copy
import re
import json

import torch
import torch.nn as nn
from torch.optim import lr_scheduler
from torchvision import datasets

from .data_transformation import data_transforms1, data_transforms2
from .training_params import resnet50_train_params, densenet121_train_params, inception_v3_train_params, mobilenet_v2_train_params
from tqdm import tqdm



class ClassificationModelTrainer():

    def __init__(self) -> None:
        self.__model_type = ""
        self.__device = "cuda" if torch.cuda.is_available() else "cpu"
        self.__data_dir = ""
        self.__data_loaders = None
        self.__class_names = None
        self.__dataset_sizes = None
        self.__dataset_name = ""
        self.__model = None
        self.__optimizer = None
        self.__lr_scheduler = None
        self.__loss_fn = nn.CrossEntropyLoss()
        self.__transfer_learning_mode = "fine_tune_all"
        self.__model_path = ""
        self.__training_params = None

    def __set_training_param(self) -> None:
        """
        Sets the required training parameters for the specified vision model.
        The default parameters used are the ones specified by the authors in
        their research paper.
        """
        if not self.__model_type:
            raise RuntimeError("The model type is not set!!!")
        self.__model = self.__training_params["model"]
        optimizer = self.__training_params["optimizer"]
        lr_decay_rate = self.__training_params["lr_decay_rate"]
        lr_step_size = self.__training_params["lr_step_size"]
        lr = self.__training_params["lr"]
        weight_decay = self.__training_params["weight_decay"]

        if self.__model_path:
            self.__set_transfer_learning_mode()
            print("==> Transfer learning enabled")
        
        # change the last linear layer to have output features of
        # same size as the number of unique classes in the new
        # dataset.
        if self.__model_type == "mobilenet_v2":
            in_features = self.__model.classifier[1].in_features
            self.__model.classifier[1] = nn.Linear(in_features, len(self.__class_names))
        elif self.__model_type == "densenet121":
            in_features = self.__model.classifier.in_features
            self.__model.classifier = nn.Linear(in_features, len(self.__class_names))
        else:
            in_features = self.__model.fc.in_features
            self.__model.fc = nn.Linear(in_features, len(self.__class_names))

        self.__model.to(self.__device)
        self.__optimizer = optimizer(
                    self.__model.parameters(),
                    lr=lr,
                    momentum=0.9,
                    weight_decay=weight_decay
                )
        if lr_decay_rate and lr_step_size:
            self.__lr_scheduler = lr_scheduler.StepLR(
                                self.__optimizer,
                                gamma=lr_decay_rate,
                                step_size=lr_step_size
                            )

    def __set_transfer_learning_mode(self) -> None:

        state_dict = torch.load(self.__model_path, map_location=self.__device)
        if self.__model_type == "densenet121":
            # '.'s are no longer allowed in module names, but previous densenet layers
            # as provided by the pytorch organization has names that uses '.'s.
            pattern = re.compile(
                    r"^(.*denselayer\d+\.(?:norm|relu|conv))\.((?:[12])\."
                    "(?:weight|bias|running_mean|running_var))$"
                    )
            for key in list(state_dict.keys()):
                res = pattern.match(key)
                if res:
                    new_key = res.group(1) + res.group(2)
                    state_dict[new_key] = state_dict[key]
                    del state_dict[key]

        self.__model.load_state_dict(state_dict)

        if self.__transfer_learning_mode == "freeze_all":
            for param in self.__model.parameters():
                param.requires_grad = False

    def __load_data(self, batch_size : int = 8) -> None:
        
        if not self.__data_dir:
            raise RuntimeError("The dataset directory not yet set.")
        image_dataset = {
                        x:datasets.ImageFolder(
                                os.path.join(self.__data_dir, x),
                                data_transforms2[x] if self.__model_type=="inception_v3" else data_transforms1[x]
                            )
                        for x in ["train", "test"]
                    }
        self.__data_loaders = {
                        x:torch.utils.data.DataLoader(
                                image_dataset[x], batch_size=batch_size,
                                shuffle=True
                            )
                        for x in ["train", "test"]
                    }
        self.__dataset_sizes = {x:len(image_dataset[x]) for x in ["train", "test"]}
        self.__class_names = image_dataset["train"].classes
        self.__dataset_name = os.path.basename(self.__data_dir.rstrip(os.path.sep))

    def setDataDirectory(self, data_dir : str = "") -> None:
        """
        Sets the directory that contains the training and test dataset. The data directory should contain 'train' and 'test' subdirectories
        for the training and test datasets.

        In each of these subdirectories, each object must have a dedicated folder and the folder containing images for the object.

        The structure of the 'test' and 'train' folder must be as follows:
        
        >> train >> class1 >> class1_train_images
                    >> class2 >> class2_train_images
                    >> class3 >> class3_train_images
                    >> class4 >> class4_train_images
                    >> class5 >> class5_train_images
        >> test >> class1 >> class1_test_images
                >> class2 >> class2_test_images
                >> class3 >> class3_test_images
                >> class4 >> class4_test_images
                >> class5 >> class5_test_images

        """
        if os.path.isdir(data_dir):
            self.__data_dir = data_dir
            return
        raise ValueError("expected a path to a directory")

    def setModelAsMobileNetV2(self) -> None:
        self.__model_type = "mobilenet_v2"
        self.__training_params = mobilenet_v2_train_params()

    def setModelAsResNet50(self) -> None:
        self.__model_type = "resnet50"
        self.__training_params = resnet50_train_params()

    def setModelAsInceptionV3(self) -> None:
        self.__model_type = "inception_v3"
        self.__training_params = inception_v3_train_params()

    def setModelAsDenseNet121(self) -> None:
        self.__model_type = "densenet121"
        self.__training_params = densenet121_train_params()

    def freezeAllLayers(self) -> None:
        """
        Set the transfer learning mode to freeze all layers.

        NOTE: The last layer (fully connected layer) is trainable.
        """
        self.__transfer_learning_mode = "freeze_all"

    def fineTuneAllLayers(self) -> None:
        """
        Sets the transfer learning mode to fine-tune the pretrained weights
        """
        self.__transfer_learning_mode = "fine_tune_all"

    def trainModel(
                self,
                num_experiments : int = 100,
                batch_size : int = 8,
                model_directory  : str = None,
                transfer_from_model: str = None,
                verbose : bool = True
            ) -> None:

        # Load dataset
        self.__load_data(batch_size)

        # Check and effect transfer learning if enabled
        if transfer_from_model:
            self.__model_path = transfer_from_model

        # Load training parameters for the specified model type
        self.__set_training_param()

        
        # Create output directory to save trained models and json mappings
        if not model_directory:
            model_directory = os.path.join(self.__data_dir, "models")

        if not os.path.exists(model_directory):
            os.mkdir(model_directory)
        
        # Dump class mappings to json file
        with open(os.path.join(model_directory, f"{self.__dataset_name}_model_classes.json"), "w") as f:
            classes_dict = {}
            class_list = sorted(self.__class_names)
            for i in range(len(class_list)):
                classes_dict[str(i)] = class_list[i]
            json.dump(classes_dict, f)

        # Prep model weights for training
        since = time.time()

        best_model_weights = copy.deepcopy(self.__model.state_dict())
        best_acc = 0.0
        prev_save_name, recent_save_name = "", ""

        # Device check and log
        print("=" * 50)
        print("Training with GPU") if self.__device == "cuda" else print("Training with CPU. This might cause slower train.")
        print("=" * 50)



        for epoch in range(num_experiments):
            if verbose:
                print(f"Epoch {epoch}/{num_experiments - 1}", "-"*10, sep="\n")

            # each epoch has a training and test phase
            for phase in ["train", "test"]:
                if phase == "train":
                    self.__model.train()
                else:
                    self.__model.eval()

                running_loss = 0.0
                running_corrects = 0

                # Iterate on the dataset in batches
                for imgs, labels in tqdm(self.__data_loaders[phase]):
                    imgs = imgs.to(self.__device)
                    labels = labels.to(self.__device)

                    self.__optimizer.zero_grad()

                    with torch.set_grad_enabled(phase == "train"):
                        output = self.__model(imgs)
                        if self.__model_type == "inception_v3":
                            output = output.logits
                        _, preds = torch.max(output, 1)
                        loss = self.__loss_fn(output, labels)

                        if phase=="train":
                            loss.backward()
                            self.__optimizer.step()
                    running_loss += loss.item() * imgs.size(0)
                    running_corrects += torch.sum(preds==labels.data)

                # Compute accuracy and loss metrics post epoch training
                if phase == "train" and isinstance(self.__lr_scheduler, torch.optim.lr_scheduler.StepLR):
                    self.__lr_scheduler.step()

                epoch_loss = running_loss / self.__dataset_sizes[phase]
                epoch_acc = running_corrects.double() / self.__dataset_sizes[phase]

                if verbose:
                    print(f"{phase} Loss: {epoch_loss:.4f} Accuracy: {epoch_acc:.4f}")
                if phase == "test" and epoch_acc > best_acc:
                    best_acc = epoch_acc
                    recent_save_name = self.__model_type+f"-{self.__dataset_name}-test_acc_{best_acc:.5f}_epoch-{epoch}.pt"
                    if prev_save_name:
                        os.remove(os.path.join(model_directory, prev_save_name))
                    best_model_weights = copy.deepcopy(self.__model.state_dict())
                    torch.save(
                            best_model_weights, os.path.join(model_directory, recent_save_name)
                        )
                    prev_save_name = recent_save_name
            

        time_elapsed = time.time() - since
        print(f"Training completed in {time_elapsed//60:.0f}m {time_elapsed % 60:.0f}s")
        print(f"Best test accuracy: {best_acc:.4f}")