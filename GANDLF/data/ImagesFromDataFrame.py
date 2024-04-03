from typing import Optional, Union
import os
from pathlib import Path
import numpy as np
import logging

import pandas
import torch
import torchio
from torchio.transforms import Pad
import SimpleITK as sitk
from tqdm import tqdm

from GANDLF.utils import (
    perform_sanity_check_on_subject,
    resize_image,
    get_filename_extension_sanitized,
    get_correct_padding_size,
    setup_logger,
)
from .preprocessing import get_transforms_for_preprocessing
from .augmentation import global_augs_dict

global_sampler_dict = {
    "uniform": torchio.data.UniformSampler,
    "uniformsampler": torchio.data.UniformSampler,
    "uniformsample": torchio.data.UniformSampler,
    "label": torchio.data.LabelSampler,
    "labelsampler": torchio.data.LabelSampler,
    "labelsample": torchio.data.LabelSampler,
    "weighted": torchio.data.WeightedSampler,
    "weightedsampler": torchio.data.WeightedSampler,
    "weightedsample": torchio.data.WeightedSampler,
}


# This function takes in a dataframe, with some other parameters and returns the dataloader
def ImagesFromDataFrame(
    dataframe: pandas.DataFrame,
    parameters: dict,
    train: bool,
    apply_zero_crop: Optional[bool] = False,
    loader_type: Optional[str] = None,
) -> Union[torchio.SubjectsDataset, torchio.Queue]:
    """
    Reads the pandas dataframe and gives the dataloader to use for training/validation/testing.

    Args:
        dataframe (pandas.DataFrame): The main input dataframe which is calculated after splitting the data CSV.
        parameters (dict): The parameters dictionary.
        train (bool): If the dataloader is for training or not.
        apply_zero_crop (Optional[bool], optional): Whether to apply zero crop or not. Defaults to False.
        loader_type (Optional[str], optional): The type of loader. Defaults to None.

    Returns:
        Union[torchio.SubjectsDataset, torchio.Queue]: The dataloader queue for validation/testing (where patching and data augmentation is not required) or the subjects dataset for training.
    """
    loader_type = loader_type if loader_type is not None else ""
    # store in previous variable names
    patch_size = parameters["patch_size"]
    headers = parameters["headers"]
    q_max_length = parameters["q_max_length"]
    q_samples_per_volume = parameters["q_samples_per_volume"]
    q_num_workers = parameters["q_num_workers"]
    q_verbose = parameters["q_verbose"]
    augmentations = parameters["data_augmentation"]
    preprocessing = parameters["data_preprocessing"]
    in_memory = parameters["in_memory"]
    sampler = parameters["patch_sampler"]

    # Finding the dimension of the dataframe for computational purposes later
    num_row, num_col = dataframe.shape
    # changing the column indices to make it easier
    dataframe.columns = range(0, num_col)
    dataframe.index = range(0, num_row)
    # This list will later contain the list of subjects
    subjects_list = []
    subjects_with_error = []

    channelHeaders = headers["channelHeaders"]
    labelHeader = headers["labelHeader"]
    predictionHeaders = headers["predictionHeaders"]
    subjectIDHeader = headers["subjectIDHeader"]

    if "logger_name" in parameters:
        logger = logging.getLogger(parameters["logger_name"])
    else:
        logger, parameters["logs_dir"], parameters["logger_name"] = setup_logger(
            output_dir=parameters["output_dir"],
            verbose=parameters.get("verbose", False),
        )

    resize_images_flag = False
    # if resize has been defined but resample is not (or is none)
    if not (preprocessing is None):
        for key in preprocessing.keys():
            # check for different resizing keys
            if key in ["resize", "resize_image", "resize_images"]:
                if not (preprocessing[key] is None):
                    resize_images_flag = True
                    preprocessing["resize_image"] = preprocessing[key]
                    break

    # helper function to save the resized images
    def _save_resized_images(
        resized_image, output_dir, subject_id, channel_str, loader_type, extension
    ):
        """
        Helper function to save the resized images

        Args:
            resized_image (sitk.Image): The resized image.
            output_dir (str): The output directory.
            subject_id (str): The subject ID.
            channel_str (str): The channel string.
            loader_type (str): The loader type.
            extension (str): The extension of the image.
        """
        # save img_resized to disk
        save_dir_for_resized_images = os.path.join(
            output_dir, loader_type + "_resized_images"
        )
        Path(save_dir_for_resized_images).mkdir(parents=True, exist_ok=True)
        save_path = os.path.join(
            save_dir_for_resized_images,
            subject_id + "_" + channel_str + "_resized" + extension,
        )
        if not os.path.isfile(save_path):
            sitk.WriteImage(resized_image, save_path)

    # iterating through the dataframe and sending logs to file
    log_file = os.path.join(parameters["logs_dir"], "data_loop.log")
    with open(log_file, "a") as fl:
        for patient in tqdm(
            range(num_row),
            file=fl,
            desc="Constructing queue for " + loader_type + " data",
        ):
            # We need this dict for storing the meta data for each subject
            # such as different image modalities, labels, any other data
            subject_dict = {}
            subject_dict["subject_id"] = str(dataframe[subjectIDHeader][patient])
            skip_subject = False
            # iterating through the channels/modalities/timepoints of the subject
            for channel in channelHeaders:
                # sanity check for malformed csv
                if not os.path.isfile(str(dataframe[channel][patient])):
                    skip_subject = True

                subject_dict[str(channel)] = torchio.ScalarImage(
                    dataframe[channel][patient]
                )

                # store image spacing information if not present
                if "spacing" not in subject_dict:
                    file_reader = sitk.ImageFileReader()
                    file_reader.SetFileName(str(dataframe[channel][patient]))
                    file_reader.ReadImageInformation()
                    subject_dict["spacing"] = torch.Tensor(file_reader.GetSpacing())

                # if resize_image is requested, the perform per-image resize with appropriate interpolator
                if resize_images_flag:
                    img_resized = resize_image(
                        subject_dict[str(channel)].as_sitk(),
                        preprocessing["resize_image"],
                    )
                    if parameters["memory_save_mode"]:
                        _save_resized_images(
                            img_resized,
                            parameters["output_dir"],
                            subject_dict["subject_id"],
                            str(channel),
                            loader_type,
                            get_filename_extension_sanitized(
                                str(dataframe[channel][patient])
                            ),
                        )
                    else:
                        # always ensure resized image spacing is used
                        subject_dict["spacing"] = torch.Tensor(img_resized.GetSpacing())
                        subject_dict[str(channel)] = torchio.ScalarImage.from_sitk(
                            img_resized
                        )

            # # for regression -- this logic needs to be thought through
            # if predictionHeaders:
            #     # get the mask
            #     if (subject_dict['label'] is None) and (class_list is not None):
            #         sys.exit('The \'class_list\' parameter has been defined but a label file is not present for patient: ', patient)

            if labelHeader is not None:
                if not os.path.isfile(str(dataframe[labelHeader][patient])):
                    skip_subject = True

                subject_dict["label"] = torchio.LabelMap(
                    dataframe[labelHeader][patient]
                )
                subject_dict["path_to_metadata"] = str(dataframe[labelHeader][patient])

                # if resize is requested, the perform per-image resize with appropriate interpolator
                if resize_images_flag:
                    img_resized = resize_image(
                        subject_dict["label"].as_sitk(),
                        preprocessing["resize_image"],
                        sitk.sitkNearestNeighbor,
                    )
                    if parameters["memory_save_mode"]:
                        _save_resized_images(
                            img_resized,
                            parameters["output_dir"],
                            subject_dict["subject_id"],
                            "label",
                            loader_type,
                            get_filename_extension_sanitized(
                                str(dataframe[channel][patient])
                            ),
                        )
                    else:
                        subject_dict["label"] = torchio.LabelMap.from_sitk(img_resized)

            else:
                subject_dict["label"] = "NA"
                subject_dict["path_to_metadata"] = str(dataframe[channel][patient])

            # iterating through the values to predict of the subject
            valueCounter = 0
            for values in predictionHeaders:
                # assigning the dict key to the channel
                subject_dict["value_" + str(valueCounter)] = np.array(
                    dataframe[values][patient]
                )
                valueCounter += 1

            # skip subject the condition was tripped
            if not skip_subject:
                # Initializing the subject object using the dict
                subject = torchio.Subject(subject_dict)
                # https://github.com/fepegar/torchio/discussions/587#discussioncomment-928834
                # this is causing memory usage to explode, see https://github.com/mlcommons/GaNDLF/issues/128
                logger.debug(
                    "Checking consistency of images in subject '"
                    + subject["subject_id"]
                    + "'"
                )
                try:
                    perform_sanity_check_on_subject(subject, parameters)
                except Exception as exception:
                    subjects_with_error.append(subject["subject_id"])
                    print(
                        "Subject '"
                        + subject["subject_id"]
                        + "' could not be loaded due to the following exception: {}".format(
                            type(exception).__name__
                        )
                        + "; message: {}".format(exception)
                    )

                # # padding image, but only for label sampler, because we don't want to pad for uniform
                if sampler["enable_padding"]:
                    psize_pad = get_correct_padding_size(
                        patch_size, parameters["model"]["dimension"]
                    )
                    padder = Pad(psize_pad, padding_mode=sampler["padding_mode"])
                    subject = padder(subject)

                # load subject into memory: https://github.com/fepegar/torchio/discussions/568#discussioncomment-859027
                if in_memory:
                    subject.load()

                # Appending this subject to the list of subjects
                subjects_list.append(subject)

    assert (
        subjects_with_error is not None
    ), f"The following subjects could not be loaded, please recheck or remove and retry: {subjects_with_error}"

    transformations_list = []

    # augmentations are applied to the training set only
    if train and not (augmentations is None):
        for aug in augmentations:
            aug_lower = aug.lower()
            if aug_lower in global_augs_dict:
                transformations_list.append(
                    global_augs_dict[aug_lower](augmentations[aug])
                )

    transform = get_transforms_for_preprocessing(
        parameters, transformations_list, train, apply_zero_crop
    )

    subjects_dataset = torchio.SubjectsDataset(subjects_list, transform=transform)
    if not train:
        return subjects_dataset

    # initialize the sampler
    sampler_obj = global_sampler_dict[sampler["type"]](patch_size)
    if sampler["type"] in ("weighted", "weightedsampler", "weightedsample"):
        sampler_obj = global_sampler_dict[sampler["type"]](
            patch_size, probability_map="label"
        )
    elif sampler["type"] in ("label", "labelsampler", "labelsample"):
        # if biased sampling is detected, then we need to pass the class probabilities
        if train and sampler["biased_sampling"]:
            # initialize the class probabilities dict
            label_probabilities = {}
            if "sampling_weights" in parameters:
                for class_index in parameters["sampling_weights"]:
                    label_probabilities[class_index] = parameters["sampling_weights"][
                        class_index
                    ]
            sampler_obj = global_sampler_dict[sampler["type"]](
                patch_size, label_probabilities=label_probabilities
            )
    # all of these need to be read from model.yaml
    patches_queue = torchio.Queue(
        subjects_dataset,
        max_length=q_max_length,
        samples_per_volume=q_samples_per_volume,
        sampler=sampler_obj,
        num_workers=q_num_workers,
        shuffle_subjects=True,
        shuffle_patches=True,
        verbose=q_verbose,
    )
    return patches_queue
