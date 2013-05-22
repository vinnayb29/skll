#!/usr/bin/env python
'''
Runs a bunch of sklearn jobs in parallel on the cluster given a config file.

@author: Nitin Madnani (nmadnani@ets.org)
@author: Dan Blanchard (dblanchard@ets.org)
@author: Michael Heilman (mheilman@ets.org)
'''

from __future__ import print_function, unicode_literals

import argparse
import json
import re
import os
import sys
import csv
from collections import defaultdict, namedtuple, OrderedDict

import numpy as np
import classifier
from texttable import ArraySizeError, Texttable
from six import string_types, iterkeys, iteritems, itervalues  # Python 2/3
from six.moves import configparser


# Named tuple for storing job results
ClassifierResultInfo = namedtuple('ClassifierResultInfo',
                                  ['train_set_name', 'test_set_name',
                                   'featureset', 'given_classifier', 'task',
                                   'task_results', 'grid_scores'])


def clean_path(path):
    ''' Replace all weird SAN paths with normal paths '''

    path = re.sub(r'/\.automount/\w+/SAN/NLP/(\w+)-(dynamic|static)',
                  r'/home/nlp-\1/\2', path)
    path = re.sub(r'/\.automount/[^/]+/SAN/Research/HomeResearch',
                  '/home/research', path)
    return path


def get_stat_string(class_result_dict, stat):
    '''
    Little helper for getting output for precision, recall, and f-score
    columns in confusion matrix.
    '''
    if stat in class_result_dict and class_result_dict[stat] is not None:
        return "{:.1f}%".format(class_result_dict[stat] * 100)
    else:
        return "N/A"


def print_fancy_output(result_tuples, grid_scores, output_file=sys.stdout):
    '''
    Function to take all of the results from all of the folds and print
    nice tables with the results.
    '''
    num_folds = len(result_tuples)
    accuracy_sum = 0.0
    score_sum = None
    prec_sum_dict = defaultdict(float)
    recall_sum_dict = defaultdict(float)
    f_sum_dict = defaultdict(float)
    result_table = None

    for k, ((conf_matrix, fold_accuracy, result_dict, model_params,
            score), grid_score) \
            in enumerate(zip(result_tuples, grid_scores), start=1):

        if num_folds > 1:
            print("\nFold: {}".format(k), file=output_file)
        param_out = ('{}: {}'.format(param_name, param_value)
                     for param_name, param_value in iteritems(model_params))
        print('Model parameters: {}'.format(
            ', '.join(param_out)), file=output_file)
        print('Grid search score = {:.5f}'.format(grid_score), file=output_file)

        if conf_matrix:
            classes = sorted(iterkeys(result_tuples[0][2]))
            result_table = Texttable(max_width=0)
            result_table.set_cols_align(["r"] * (len(classes) + 4))
            result_table.add_rows([[""] + classes + ["Precision",
                                                     "Recall",
                                                     "F-measure"]],
                                  header=True)

            for i, actual_class in enumerate(classes):
                conf_matrix[i][i] = "[{}]".format(conf_matrix[i][i])
                class_prec = get_stat_string(result_dict[actual_class],
                                             "Precision")
                class_recall = get_stat_string(result_dict[actual_class],
                                               "Recall")
                class_f = get_stat_string(result_dict[actual_class],
                                          "F-measure")
                if class_prec != 'N/A':
                    prec_sum_dict[actual_class] += float(class_prec[:-1])
                if class_recall != 'N/A':
                    recall_sum_dict[actual_class] += float(class_recall[:-1])
                if class_f != 'N/A':
                    f_sum_dict[actual_class] += float(class_f[:-1])
                try:
                    result_row = ([actual_class] + conf_matrix[i] +
                                  [class_prec, class_recall, class_f])
                    result_table.add_row(result_row)
                except ArraySizeError as e:
                    print(("Row does not contain enough elements.\n " +
                           "actual_class: {}\n" +
                           "conf_matrix[i]: {}\n" +
                           "class_prec: {}\n" +
                           "class_recall: {}\n" +
                           "class_f: {}\n").format(actual_class,
                                                   conf_matrix[i],
                                                   class_prec, class_recall,
                                                   class_f),
                          file=sys.stderr)
                    raise e
            print(result_table.draw(), file=output_file)
            print("(row = reference; column = predicted)", file=output_file)
            print("Accuracy = {:.1f}%".format(fold_accuracy), file=output_file)
            accuracy_sum += fold_accuracy

        if score is not None:
            if score_sum is None:
                score_sum = score
            else:
                score_sum += score
            print('Objective function score = {:.5f}'.format(
                score), file=output_file)
        print(file=output_file)

    if num_folds > 1:
        print("\nAverage:", file=output_file)
        if result_table:
            result_table = Texttable(max_width=0)
            result_table.set_cols_align(["l", "r", "r", "r"])
            result_table.add_rows(
                [["Class", "Precision", "Recall", "F-measure"]], header=True)
            for actual_class in classes:
                # Convert sums to means
                prec_mean = prec_sum_dict[actual_class] / num_folds
                recall_mean = recall_sum_dict[actual_class] / num_folds
                f_mean = f_sum_dict[actual_class] / num_folds
                result_table.add_row([actual_class] +
                                     ["{:.1f}%".format(prec_mean),
                                      "{:.1f}%".format(recall_mean),
                                      "{:.1f}%".format(f_mean)])
            print(result_table.draw(), file=output_file)
            print("Accuracy = {:.1f}%".format(accuracy_sum / num_folds),
                  file=output_file)
        if score_sum is not None:
            print("Objective function score = {:.5f}".format(score_sum
                                                             / num_folds),
                  file=output_file)


def load_featureset(dirpath, featureset, suffix):
    '''
    loads a list of feature files and merges them (or loads just one if
    featureset is a string).
    '''
    if isinstance(featureset, string_types):
        featureset = [featureset]

    example_dict = OrderedDict()
    for i, feats in enumerate(featureset):
        examples = classifier.load_examples(os.path.join(dirpath,
                                                         feats + suffix))


        # check that the IDs are unique
        ex_ids = [example['id'] for example in examples]
        if len(ex_ids) != len(set(ex_ids)):
            raise ValueError('The example IDs are not unique.')

        # check that the different feature files have the same IDs
        if i > 0:
            if set(ex_ids) != set(example_dict.keys()):
                raise ValueError('The sets of example IDs in two feature files \
                                  do not match')

        for example in examples:
            if i == 0:
                example_dict[example['id']] = example
            else:
                # Check that two feature files have unique feature names by
                # checking that the new features don't already exist 
                # (i.e., that the intersection is null set).
                if set(example['x'].keys()) \
                   & set(example_dict[example['id']]['x'].keys()):
                    raise ValueError('Two feature files have the same feature!')

                if example['y'] != example_dict[example['id']]['y']:
                    raise ValueError('Two feature files have different labels' +
                                     ' (i.e., y values) for the same ID.')

                example_dict[example['id']]['x'].update(example['x'])

    return np.array(list(itervalues(example_dict)))  # Python 2/3 compatible


def classify_featureset(jobname, featureset, given_classifier, train_path,
                        test_path, train_set_name, test_set_name, modelpath,
                        prediction_prefix, grid_search,
                        grid_objective, do_scale_features, cross_validate,
                        evaluate, suffix, log_path, probability, resultspath,
                        fixed_parameters, param_grid, pos_label_str,
                        overwrite, use_dense_features, min_feature_count,
                        grid_search_jobs, cv_folds):
    ''' Classification job to be submitted to grid '''

    with open(log_path, 'w') as log_file:
        if cross_validate:
            print("Cross-validating on {}, feature set {} ...".format(
                train_set_name, featureset), file=log_file)
        else:
            print("Training on {}, Test on {}, feature set {} ...".format(
                train_set_name, test_set_name, featureset), file=log_file)

        # load the training and test examples
        train_examples = load_featureset(train_path, featureset, suffix)
        if not cross_validate:
            test_examples = load_featureset(test_path, featureset, suffix)

        # initialize a classifer object
        learner = classifier.Classifier(probability=probability,
                                        model_type=given_classifier,
                                        do_scale_features=do_scale_features,
                                        model_kwargs=fixed_parameters,
                                        pos_label_str=pos_label_str,
                                        use_dense_features=use_dense_features,
                                        min_feature_count=min_feature_count)

        # check whether a trained model on the same data with the same
        # featureset already exists if so, load it (and the feature
        # vocabulary) and then use it on the test data
        modelfile = os.path.join(modelpath, '{}.model'.format(jobname))

        # check if we're doing cross-validation, because we only load/save
        # models when we're not.
        if not cross_validate:

            # load the model if it already exists
            if os.path.exists(modelfile) and not overwrite:
                print('\tloading pre-existing {} model: {}'.format(
                    given_classifier, modelfile))
                learner.load_model(modelfile)

            # if we have do not have a saved model, we need to train one.
            else:
                print('\tfeaturizing and training new {} model'.format(
                    given_classifier), file=log_file)

                grid_search_folds = 5
                if not isinstance(cv_folds, int):
                    grid_search_folds = cv_folds

                best_score = learner.train(train_examples,
                                           grid_search=grid_search,
                                           grid_search_folds=grid_search_folds,
                                           grid_objective=grid_objective,
                                           param_grid=param_grid,
                                           grid_jobs=grid_search_jobs)
                grid_scores = [best_score]

                # save model
                learner.save_model(modelfile)


                if grid_search:
                    print('\tbest {} score: {}'.format(grid_objective.__name__,
                                                       round(best_score, 3)),
                          file=log_file)

            # print out the tuned parameters and best CV score
            param_out = ('{}: {}'.format(param_name, param_value)
                         for param_name, param_value in
                         iteritems(learner.model.get_params()))
            print('\thyperparameters: {}'.format(', '.join(param_out)),
                  file=log_file)

        # run on test set or cross-validate on training data, depending on what
        # was asked for
        if cross_validate:
            print('\tcross-validating', file=log_file)
            results, grid_scores = learner.cross_validate(train_examples,
                                             prediction_prefix=prediction_prefix,
                                             grid_search=grid_search,
                                             cv_folds=cv_folds,
                                             grid_objective=grid_objective,
                                             param_grid=param_grid,
                                             grid_jobs=grid_search_jobs)
            task = 'cross-validate'
        elif evaluate:
            print('\tevaluating predictions', file=log_file)
            results = [learner.evaluate(
                test_examples, prediction_prefix=prediction_prefix,
                grid_objective=grid_objective)]
            task = 'evaluate'
        else:
            print('\twriting predictions', file=log_file)
            task = 'predict'
            results = None
            learner.predict(test_examples, prediction_prefix=prediction_prefix)

        # write out results to file if we're not predicting
        result_info = ClassifierResultInfo(train_set_name, test_set_name,
                                           featureset, given_classifier, task,
                                           results, grid_scores)
        if task != 'predict':
            with open(os.path.join(resultspath, '{}.results'.format(jobname)),
                      'w') as output_file:
                print_fancy_output(result_info.task_results, 
                                   result_info.grid_scores, output_file)

    return result_info


def munge_featureset_name(featureset):
    ''' Converts feature set into '''
    if isinstance(featureset, string_types):
        return featureset

    res = '+'.join(featureset)
    return res


def fix_json(json_string):
    '''
    Takes a bit of JSON that might have bad quotes or capitalized booleans
    and fixes that stuff.
    '''
    json_string = json_string.replace('True', 'true')
    json_string = json_string.replace('False', 'false')
    json_string = json_string.replace("'", '"')
    return json_string


def load_cv_folds(cv_folds_location):
    '''
    Loads CV folds from a CSV file with columns for example ID and fold ID
    (and a header).
    '''
    with open(cv_folds_location, 'rb') as f:
        reader = csv.reader(f)
        reader.next()  # discard the header
        res = {}
        for row in reader:
            res[row[0]] = row[1]

    return res


def run_configuration(config_file, local=False, overwrite=True, queue='nlp.q',
                      hosts=None):
    '''
    Takes a configuration file and runs the specified jobs on the grid.
    '''
    # initialize config parser
    config = configparser.SafeConfigParser({'test_location': '',
                                            'log': '',
                                            'results': '',
                                            'predictions': '',
                                            'grid_search': 'False',
                                            'objective': "f1_score_micro",
                                            'scale_features': 'True',
                                            'probability': 'False',
                                            'fixed_parameters': '[]',
                                            'param_grids': '[]',
                                            'pos_label_str': None,
                                            'featureset_names': '[]',
                                            'use_dense_features': 'False',
                                            'min_feature_count': '1',
                                            'grid_search_jobs': '0',
                                            'cv_folds_location': None})
    config.readfp(config_file)

    if not local:
        # import pythongrid if available
        try:
            from pythongrid import Job, process_jobs
        except ImportError:
            local = True
            print('pythongrid not available.  Forcing local mode.',
                  file=sys.stderr)

    # extract sklearn parameters from the config file
    given_classifiers = json.loads(fix_json(config.get("Input",
                                                       "classifiers")))
    given_featuresets = json.loads(fix_json(config.get("Input",
                                                       "featuresets")))
    given_featureset_names = json.loads(fix_json(config.get(
        "Input", "featureset_names")))
    fixed_parameter_list = json.loads(fix_json(config.get(
        "Input", "fixed_parameters")))
    param_grid_list = json.loads(fix_json(config.get("Tuning", "param_grids")))
    pos_label_str = config.get("Tuning", "pos_label_str")
    use_dense_features = config.getboolean("Tuning", "use_dense_features")

    # get all the input paths and directories (without trailing slashes)
    train_path = config.get("Input", "train_location").rstrip('/')
    test_path = config.get("Input", "test_location").rstrip('/')
    suffix = config.get("Input", "suffix")
    
    # get the cv folds file and make a dictionary from it
    cv_folds_location = config.get("Input", "cv_folds_location")
    if cv_folds_location:
        cv_folds = load_cv_folds(cv_folds_location)
    else:
        cv_folds = 10

    # get all the output files and directories
    resultspath = config.get("Output", "results")
    logpath = config.get("Output", "log")
    modelpath = config.get("Output", "models")
    probability = config.getboolean("Output", "probability")

    # do we want to keep the predictions?
    prediction_dir = config.get("Output", "predictions")
    if prediction_dir:
        os.system("mkdir -p {}".format(prediction_dir))

    # make sure log path exists
    if logpath:
        os.system("mkdir -p {}".format(logpath))

    # make sure results path exists
    if resultspath:
        os.system("mkdir -p {}".format(resultspath))

    # make sure all the specified paths exist
    if not os.path.exists(train_path):
        print(("Error: the training path specified in config file ({}) does " +
               "not exist.").format(train_path), file=sys.stderr)
        sys.exit(2)
    if test_path and not os.path.exists(test_path):
        print(("Error: the test path specified in config file ({}) does " +
               "not exist.").format(test_path), file=sys.stderr)
        sys.exit(2)

    # do we need to run a grid search for the hyperparameters or are we just
    # using the defaults
    do_grid_search = config.getboolean("Tuning", "grid_search")

    # the minimum number of examples a feature must be nonzero in to be included
    min_feature_count = config.getint("Tuning", "min_feature_count")

    # how many jobs should we run in parallel for grid search
    grid_search_jobs = config.getint("Tuning", "grid_search_jobs")
    if not grid_search_jobs:
        grid_search_jobs = None

    # what is the objective function for the grid search?
    grid_objective_func = config.get("Tuning", "objective")
    if grid_objective_func not in {'f1_score_micro', 'f1_score_macro',
                                   'accuracy', 'f1_score_least_frequent',
                                   'spearman', 'pearson', 'kendall_tau',
                                   'quadratic_weighted_kappa',
                                   'unweighted_kappa'}:
        print('Error: invalid grid objective function.', file=sys.stderr)
        sys.exit(2)
    else:
        grid_objective = getattr(classifier, grid_objective_func)

    # do we need to scale the feature values?
    do_scale_features = config.getboolean("Tuning", "scale_features")

    # are we doing cross validation or actual testing or just generating
    # predictions on a new test set? If no test set was specified then assume
    # that we are doing cross validation. If the results field was not
    # specified then assume that we are just generating predictions
    evaluate = False
    cross_validate = False
    predict = False
    if test_path and resultspath:
        evaluate = True
    elif not test_path:
        cross_validate = True
    else:
        predict = True

    if cross_validate:
        task = 'cross-validate'
    elif evaluate:
        task = 'evaluate'
    else:
        task = 'predict'

    # make sure that, if we are in prediction mode, we have a prediction_dir
    if predict and not prediction_dir:
        print('Error: you need to specify a prediction directory if you are ' +
              'using prediction mode (no "results" option in config file).',
              file=sys.stderr)
        sys.exit(2)

    # the list of jobs submitted (if running on grid)
    if not local:
        jobs = []

    if not given_featureset_names:
        given_featureset_names = [munge_featureset_name(
            x) for x in given_featuresets]
    assert len(given_featureset_names) == len(given_featuresets)

    # For each feature set
    for featureset, featureset_name in zip(given_featuresets,
                                           given_featureset_names):

        # and for each classifier
        for classifier_num, given_classifier in enumerate(given_classifiers):

            # store training/test set names for later use
            train_set_name = os.path.basename(train_path)
            test_set_name = os.path.basename(test_path) if test_path else "cv"

            # create a name for the job
            name_components = [train_set_name, test_set_name,
                               featureset_name, given_classifier]

            # add scaling information to name
            if do_scale_features:
                name_components.append('scaled')
            else:
                name_components.append('unscaled')

            # add tuning information to name
            if do_grid_search:
                name_components.append('tuned')
                name_components.append(grid_objective.__name__)
            else:
                name_components.append('untuned')

            # add task name
            name_components.append(task)

            jobname = '_'.join(name_components)

            # change the prediction prefix to include the feature set
            prediction_prefix = os.path.join(prediction_dir, jobname)

            # the log file that stores the actual output of this script (e.g.,
            # the tuned parameters, what kind of experiment was run, etc.)
            temp_logfile = os.path.join(logpath, '{}.log'.format(jobname))

            # create job if we're doing things on the grid
            job_args = [jobname, featureset, given_classifier, train_path,
                        test_path, train_set_name, test_set_name, modelpath,
                        prediction_prefix, do_grid_search,
                        grid_objective, do_scale_features, cross_validate,
                        evaluate, suffix, temp_logfile, probability,
                        resultspath, (fixed_parameter_list[classifier_num]
                                      if fixed_parameter_list else dict()),
                        (param_grid_list[classifier_num] if param_grid_list
                         else None),
                        pos_label_str, overwrite, use_dense_features,
                        min_feature_count, grid_search_jobs, cv_folds]
            if not local:
                jobs.append(Job(classify_featureset, job_args,
                                num_slots=(5 if do_grid_search else 1),
                                name=jobname, queue=queue))
            else:
                classify_featureset(*job_args)

    # submit the jobs (if running on grid)
    if not local:
        job_results = process_jobs(jobs, white_list=hosts)

        # Check for errors
        for result_info in job_results:
            if not hasattr(result_info, 'task'):
                print('There was an error running the experiment:\n' +
                      '{}'.format(result_info), file=sys.stderr)
                sys.exit(2)


if __name__ == '__main__':
    # Get command line arguments
    parser = argparse.ArgumentParser(
        description="Runs sklearn jobs in parallel on the cluster given a \
                     config file.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        conflict_handler='resolve')
    parser.add_argument('config_file',
                        help='Configuration file describing the sklearn task\
                              to run.',
                        type=argparse.FileType('r'))
    parser.add_argument('-k', '--keep_models',
                        help='If models and/or vocabs exists, re-use them\
                              instead of overwriting them.',
                        action='store_true')
    parser.add_argument('-l', '--local',
                        help='Do not use the Grid Engine for running jobs and\
                              just run everything sequential on the local \
                              machine. This is for debugging.',
                        action='store_true')
    parser.add_argument('-m', '--machines',
                        help="comma-separated list of machines to add to\
                              pythongrid's whitelist (if not specified, all\
                              available machines are used). Note that full \
                              names must be specified, e.g., \
                              \"nlp.research.ets.org\"",
                        type=str, default=None)
    parser.add_argument('-q', '--queue',
                        help="Use this queue for python grid.",
                        type=str, default='nlp.q')

    args = parser.parse_args()
    machines = None
    if args.machines:
        machines = args.machines.split(',')
    run_configuration(args.config_file,
                      local=args.local,
                      overwrite=not args.keep_models,
                      queue=args.queue,
                      hosts=machines)
