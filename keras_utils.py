import pandas as pd
import numpy as np
import os
import pickle
import glob
import ast
import re

np.random.seed(1000)

from sklearn_utils import load_both, load_obama, load_romney, classification_report, f1_score, accuracy_score, \
    confusion_matrix
from sklearn.model_selection import StratifiedKFold

from keras.preprocessing.text import Tokenizer
from keras.preprocessing.sequence import pad_sequences
from keras.callbacks import ModelCheckpoint, ReduceLROnPlateau
from keras.utils.np_utils import to_categorical
from keras.models import Model
from keras import backend as K

train_obama_path = "data/obama_csv.csv"
train_romney_path = "data/romney_csv.csv"

train_obama_full_path = "data/full_obama_csv.csv"
train_romney_full_path = "data/full_romney_csv.csv"

test_obama_path = "data/obama_csv_test.csv"
test_romney_path = "data/romney_csv_test.csv"

model_dirs = ['conv/', 'n_conv/', 'lstm/', 'bidirectional_lstm/', 'multiplicative_lstm/']


def fbeta_score(y_true, y_pred):
    '''
    Computes the fbeta score. For ease of use, beta is set to 1.
    Therefore always computes f1_score
    '''
    def recall(y_true, y_pred):
        """Recall metric.

        Only computes a batch-wise average of recall.

        Computes the recall, a metric for multi-label classification of
        how many relevant items are selected.
        """
        true_positives = K.sum(K.round(K.clip(y_true * y_pred, 0, 1)))
        possible_positives = K.sum(K.round(K.clip(y_true, 0, 1)))
        recall = true_positives / (possible_positives + K.epsilon())
        return recall

    def precision(y_true, y_pred):
        """Precision metric.

        Only computes a batch-wise average of precision.

        Computes the precision, a metric for multi-label classification of
        how many selected items are relevant.
        """
        true_positives = K.sum(K.round(K.clip(y_true * y_pred, 0, 1)))
        predicted_positives = K.sum(K.round(K.clip(y_pred, 0, 1)))
        precision = true_positives / (predicted_positives + K.epsilon())
        return precision

    precision = precision(y_true, y_pred)
    recall = recall(y_true, y_pred)
    return 2 * ((precision * recall) / (precision + recall))


def load_embedding_matrix(embedding_path, word_index, max_nb_words, embedding_dim, print_error_words=True):
    if not os.path.exists('data/embedding_matrix max words %d embedding dim %d.npy' % (max_nb_words, embedding_dim)):
        embeddings_index = {}
        error_words = []

        print("Creating embedding matrix")
        print("Loading : ", embedding_path)

        f = open(embedding_path, encoding='utf8')
        for line in f:
            values = line.split()
            word = values[0]
            try:
                coefs = np.asarray(values[1:], dtype='float32')
                embeddings_index[word] = coefs
            except Exception:
                error_words.append(word)

        f.close()

        if len(error_words) > 0:
            print("%d words could not be added." % (len(error_words)))
            if print_error_words:
                print("Words are : \n", error_words)

        print('Preparing embedding matrix.')

        # prepare embedding matrix
        nb_words = min(max_nb_words, len(word_index))
        embedding_matrix = np.zeros((nb_words, embedding_dim))
        for word, i in word_index.items():
            if i >= nb_words:
                continue
            embedding_vector = embeddings_index.get(word)
            if embedding_vector is not None:
                # words not found in embedding index will be all-zeros.
                embedding_matrix[i] = embedding_vector

        np.save('data/embedding_matrix max words %d embedding dim %d.npy' % (max_nb_words,
                                                                             embedding_dim),
                embedding_matrix)

        print('Saved embedding matrix')

    else:
        embedding_matrix = np.load('data/embedding_matrix max words %d embedding dim %d.npy' % (max_nb_words,
                                                                                                embedding_dim))

        print('Loaded embedding matrix')

    return embedding_matrix


def create_ngram_set(input_list, ngram_value=2):
    return set(zip(*[input_list[i:] for i in range(ngram_value)]))


def add_ngram(sequences, token_indice, ngram_range=2):
    new_sequences = []
    for input_list in sequences:
        new_list = input_list[:]
        for i in range(len(new_list) - ngram_range + 1):
            for ngram_value in range(2, ngram_range + 1):
                ngram = tuple(new_list[i:i + ngram_value])
                if ngram in token_indice:
                    new_list.append(token_indice[ngram])
        new_sequences.append(new_list)

    return new_sequences


def prepare_tokenized_data(texts, max_nb_words, max_sequence_length, ngram_range=2):
    if not os.path.exists('data/tokenizer.pkl'):
        tokenizer = Tokenizer(num_words=max_nb_words)
        tokenizer.fit_on_texts(texts)

        with open('data/tokenizer.pkl', 'wb') as f:
            pickle.dump(tokenizer, f)

        print('Saved tokenizer.pkl')
    else:
        with open('data/tokenizer.pkl', 'rb') as f:
            tokenizer = pickle.load(f)
            print('Loaded tokenizer.pkl')

    sequences = tokenizer.texts_to_sequences(texts)
    word_index = tokenizer.word_index
    print('Found %s unique 1-gram tokens.' % len(word_index))

    ngram_set = set()
    for input_list in sequences:
        for i in range(2, ngram_range + 1):
            set_of_ngram = create_ngram_set(input_list, ngram_value=i)
            ngram_set.update(set_of_ngram)

    # Dictionary mapping n-gram token to a unique integer.
    # Integer values are greater than max_features in order
    # to avoid collision with existing features.
    start_index = max_nb_words + 1
    token_indice = {v: k + start_index for k, v in enumerate(ngram_set)}
    indice_token = {token_indice[k]: k for k in token_indice}
    word_index.update(token_indice)

    max_features = np.max(list(indice_token.keys())) + 1
    print('Now there are:', max_features, 'features')

    # Augmenting X_train and X_test with n-grams features
    sequences = add_ngram(sequences, token_indice, ngram_range)
    print('Average sequence length: {}'.format(np.mean(list(map(len, sequences)), dtype=int)))
    print('Max sequence length: {}'.format(np.max(list(map(len, sequences)))))

    data = pad_sequences(sequences, maxlen=max_sequence_length)

    return (data, word_index)


def train_keras_model_cv(model_gen, model_fn, max_nb_words=16000, max_sequence_length=140,
                         k_folds=3, nb_epoch=40, batch_size=100, seed=1000):
    data, labels, texts, word_index = prepare_data(max_nb_words, max_sequence_length)

    print("Dataset :", data.shape)
    skf = StratifiedKFold(k_folds, shuffle=True, random_state=seed)

    fbeta_scores = []

    for i, (train_idx, test_idx) in enumerate(skf.split(texts, labels)):
        x_train, y_train = data[train_idx, :], labels[train_idx]
        x_test, y_test = data[test_idx, :], labels[test_idx]

        y_train_categorical = to_categorical(np.asarray(y_train))
        y_test_categorical = to_categorical(np.asarray(y_test))

        K.clear_session()

        model = model_gen()  # type: Model
        model.compile(loss='categorical_crossentropy', optimizer='adam', metrics=['acc', fbeta_score])

        model_checkpoint = ModelCheckpoint('models/%s-cv-%d.h5' % (model_fn, i + 1), monitor='val_fbeta_score',
                                           verbose=2,
                                           save_weights_only=True,
                                           save_best_only=True, mode='max')

        reduce_lr = ReduceLROnPlateau(monitor='val_fbeta_score', patience=5, mode='max',
                                      factor=0.8, cooldown=5, min_lr=1e-6, verbose=2)

        model.fit(x_train, y_train_categorical, validation_data=(x_test, y_test_categorical),
                  callbacks=[model_checkpoint, reduce_lr], nb_epoch=nb_epoch, batch_size=batch_size)

        model.load_weights('models/%s-cv-%d.h5' % (model_fn, i + 1))

        scores = model.evaluate(x_test, y_test_categorical, batch_size=batch_size)
        fbeta_scores.append(scores[-1])

        print('\nF1 Scores of Cross Validation %d: %0.4f' % (i + 1, scores[-1]))

        del model

    print("Average fbeta score : ", sum(fbeta_scores) / len(fbeta_scores))

    with open('models/%s-scores.txt' % (model_fn), 'w') as f:
        f.write(str(fbeta_scores))


def train_full_model(model_gen, model_fn, max_nb_words=16000, max_sequence_length=140, use_full_data=False,
                     nb_epoch=40, batch_size=100, seed=1000):
    np.random.seed(seed)
    data, labels, texts, word_index = prepare_data(max_nb_words, max_sequence_length, use_full_data)

    labels_categorical = to_categorical(np.asarray(labels))

    model = model_gen()
    model.compile(loss='categorical_crossentropy', optimizer='adam', metrics=['accuracy', fbeta_score])

    model_checkpoint = ModelCheckpoint('models/%s-final.h5' % (model_fn), monitor='val_fbeta_score', verbose=2,
                                       save_weights_only=True,
                                       save_best_only=True, mode='max')

    reduce_lr = ReduceLROnPlateau(monitor='val_fbeta_score', patience=5, mode='max',
                                  factor=0.8, cooldown=5, min_lr=1e-6, verbose=2)

    model.fit(data, labels_categorical, validation_data=(data, labels_categorical),
              callbacks=[model_checkpoint, reduce_lr], nb_epoch=nb_epoch, batch_size=batch_size)

    print('Finished Training model')

    model.load_weights('models/%s-final.h5' % (model_fn))

    scores = model.evaluate(texts, labels_categorical, batch_size=batch_size)
    print('\nTraining F1 Scores of Cross Validation: %0.4f' % (scores[-1]))


def prepare_data(max_nb_words, max_sequence_length, mode='train', dataset='full'):
    assert dataset in ['full', 'obama', 'romney']

    print('Loading %s data' % mode)

    if dataset == 'full':
        texts, labels, label_map = load_both(mode)
    elif dataset == 'obama':
        texts, labels, label_map = load_obama(mode)
    else:
        texts, labels, label_map = load_romney(mode)

    print('Tokenizing texts')
    data, word_index = prepare_tokenized_data(texts, max_nb_words, max_sequence_length)
    print('Finished tokenizing texts')
    print('-' * 80)
    return data, labels, texts, word_index


def get_keras_scores(normalize_scores=False):
    clf_scores = []

    for m, model_dir in enumerate(model_dirs):
        weights_path = 'models/' + model_dir + '*.txt'

        weight_path = glob.glob(weights_path)
        print('Loading score file [0]:', weight_path)

        with open(weight_path[0], 'r') as f:
            clf_weight_data = ast.literal_eval(f.readline())

        clf_scores.extend(clf_weight_data)

    if normalize_scores:
        weight_sum = np.sum(np.asarray(clf_scores, dtype=np.float32))
        weights = [w / weight_sum for w in clf_scores]
        clf_scores = weights

    return clf_scores


def get_predictions_keras_models(models, data, save_dir, normalize_weights=False):
    model_preds = []
    clf_scores = []

    assert len(models) == len(model_dirs), 'Number of provided models must match ' \
                                           'number of model directories specified in keras_utils'

    for m, model_dir in enumerate(model_dirs):
        path = 'models/' + model_dir + '*.h5'
        weights_path = 'models/' + model_dir + '*.txt'

        weight_path = glob.glob(weights_path)
        print('Loading weight file [0]:', weight_path)

        with open(weight_path[0], 'r') as f:
            clf_weight_data = ast.literal_eval(f.readline())

        fns = glob.glob(path)
        cv_ids = []
        for i in range(len(fns)):
            fn = fns[i]
            cv_id = re.search(r'\d+', fn).group()
            cv_ids.append(int(cv_id))

        clf_weight_data = [clf_weight_data[i - 1] for i in cv_ids]
        clf_scores.extend(clf_weight_data)

        model = models[m]  # type: Model

        temp_preds = np.zeros((len(cv_ids), data.shape[0], 3))

        for j, fn in enumerate(fns):
            model.load_weights(fn)
            preds = model.predict(data, batch_size=100)
            temp_preds[j, :, :] = preds

            print('Got predictions for model - %s' % (fn))

        model_preds.append(temp_preds)  # temp_preds.mean(axis=0)

        preds_save_path = save_dir + "/" + model_dir + os.path.splitext(os.path.basename(weight_path[0]))[0] + '.npy'
        preds = temp_preds.mean(axis=0)

        np.save(preds_save_path, preds)
        print('Saved predictions for %s in %s' % (model_dir[:-1], preds_save_path))

        print()

    if normalize_weights:
        weight_sum = np.sum(np.asarray(clf_scores, dtype=np.float32))
        weights = [w / weight_sum for w in clf_scores]
        clf_scores = weights

    return (model_preds, clf_scores)


if __name__ == '__main__':
    max_nb_words = 90046
    max_sequence_length = 65

    data, labels, texts, word_index = prepare_data(max_nb_words, max_sequence_length)

    print(data.shape)
    print(data.dtype)
    print(data[0])
    print('\n', '*' * 80, '\n')
    print(data[1])
    pass
