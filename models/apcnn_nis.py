#!/usr/bin/python3.6
#encoding=utf-8
#pytorch==0.4.1
import argparse
import torch
import pickle
import torch.nn as nn
import torch.optim as optim
import torch.nn.init as init
import torch.nn.functional as F
import numpy as np
from utils import *
from preprocess import load_bags
from modules import Embedding, PCNN, Attention, NIS
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

class APCNN_NIS(nn.Module):
    def __init__(self, class_num, word_embedding_matrix, position1_embedding_matrix, \
        position2_embedding_matrix, filters, kernel_size, padding=0, activation=nn.Tanh(), \
        dropout=0.5, nis_hidden_dims=[]):
        super(APCNN_NIS, self).__init__()
        self.filters = filters
        word_dim = word_embedding_matrix.shape[1]
        self.embedding = Embedding(word_embedding_matrix, position1_embedding_matrix, position2_embedding_matrix)
        self.pcnn = PCNN(1, filters, kernel_size, padding=padding, activation=activation, dropout=dropout)
        self.attention = Attention(3 * filters + word_dim, activation=activation)
        self.attention_weight = None
        self.nis = NIS(3 * filters, nis_hidden_dims)
        self.linear = nn.Linear(3 * filters, class_num, bias=True)

    def init_params(self):
        init.xavier_uniform_(self.pcnn.cnn.weight)
        init.constant_(self.pcnn.cnn.bias, 0)
        for i, param in enumerate(self.nis.linears.parameters()):
            if i % 2 == 0:
                init.xavier_uniform_(param)
            else:
                init.constant_(param, 0)
        init.xavier_uniform_(self.linear.weight)
        init.constant_(self.linear.bias, 0)
        init.xavier_uniform_(self.attention.linear.weight)
        init.constant_(self.attention.linear.bias, 0)

    def forward(self, sentence, position1, position2, entity_pos, entity_pair):
        word_seq = torch.LongTensor(sentence).to(device)
        pos1_seq = torch.LongTensor(position1).to(device)
        pos2_seq = torch.LongTensor(position2).to(device)
        epos_seq = torch.LongTensor(entity_pos).to(device)
        epair_seq = torch.LongTensor([entity_pair]).to(device)
        embedded_inputs = self.embedding(word_seq, pos1_seq, pos2_seq)
        pcnn_output = self.pcnn(embedded_inputs, epos_seq)
        pcnn_feature = pcnn_output.view(-1, 3 * self.filters)
        bag_size = pcnn_feature.shape[0]
        if bag_size == 1:
            output = self.linear(pcnn_feature)
        else:
            masked_feature = self.nis(pcnn_feature)
            masked_bag_size = masked_feature.shape[0]
            if masked_bag_size == 1:
                output = self.linear(masked_feature)
            else:
                entity_embedding = self.embedding.word_embedding(epair_seq).squeeze()
                self.attention_weight = self.attention(masked_feature, entity_embedding[0], entity_embedding[1])
                weighted_masked_feature = torch.mul(masked_feature, self.attention_weight.unsqueeze(-1))
                bag_masked_feature = torch.sum(weighted_masked_feature, dim=0, keepdim=True)
                output = self.linear(bag_masked_feature)
        return output

def predict(model, relations, counts, sents, poss, eposs, epair, seq_len):
    num_bag = len(relations)
    predict_y = np.zeros((num_bag), dtype=np.int32)
    predict_y_prob = np.zeros((num_bag), dtype=np.float32)
    y = np.array(relations, dtype=np.int32)
    with torch.no_grad():
        model.eval()
        for bag_idx, instance_relation in enumerate(relations):
            ins_count = counts[bag_idx]
            ins_sentence = np.array(sents[bag_idx], dtype=np.int32).reshape((ins_count, seq_len))
            ins_pos1 = np.array([poss[bag_idx][m][0] for m in range(ins_count)], dtype=np.int32).reshape((ins_count, seq_len))
            ins_pos2 = np.array([poss[bag_idx][m][1] for m in range(ins_count)], dtype=np.int32).reshape((ins_count, seq_len))
            ins_epos = np.array(eposs[bag_idx], dtype=np.int32).reshape((ins_count, 2))
            ins_epair = epair[bag_idx]
            result = model(ins_sentence, ins_pos1, ins_pos2, ins_epos, ins_epair)
            result = F.softmax(result, dim=1)
            if torch.cuda.is_available():
                max_prob, max_label = list(map(lambda x: x.detach().cpu().numpy(), torch.max(result, dim=1)))
            else:
                max_prob, max_label = list(map(lambda x: x.detach().numpy(), torch.max(result, dim=1)))
            predict_y[bag_idx] = max_label
            predict_y_prob[bag_idx] = max_prob
        model.train()
    return predict_y, predict_y_prob, y

def train(snapshot_path, prfile_path, verbose=True, logger=None):
    parser = argparse.ArgumentParser(description="Train APCNN+NIS model.")
    parser.add_argument("--model", nargs='?', default="apcnn", help="Name of model.")
    parser.add_argument("--data_path", nargs='?', default="../data/processed", help="Path of input data.")
    parser.add_argument("--output_path", nargs='?', default="../results", help="Path of output log file, snapshot and prfile.")
    parser.add_argument("--filters", type=int, default=230, help="Number of convolutional filter channels.")
    parser.add_argument("--kernel_size", type=int, default=3, help="Size of convolutional filter.")
    parser.add_argument("--class_num", type=int, default=53, help="Number of relations.")
    parser.add_argument("--seq_len", type=int, default=80, help="Length of sentences.")
    parser.add_argument("--batch_size", type=int, default=100, help="Batch size.")
    parser.add_argument("--epochs", type=int, default=20, help="Number of training epochs.")
    parser.add_argument("--nis_hidden_dims", nargs="?", default="512, 256, 128, 64", help="Dimensions of NIS hidden layers.")
    args = parser.parse_args()
    arg_dict = vars(args)
    if verbose:
        logger = logger if logger is not None else print
        length = max([len(arg) for arg in arg_dict.keys()])
        for arg, value in arg_dict.items():
            logger("%s | %s" % (arg.ljust(length).replace('_', ' '), str(value)))
    logger("Init Variables.")
    dictionary = pickle.load(open(os.path.join(args.data_path, 'dictionary.p'), 'rb'))
    nis_hidden_dims = [int(x) for x in args.nis_hidden_dims.split(',') if x]
    w = np.load(os.path.join(args.data_path, "word_vector.npy"))
    w[1:, :] = w[1:, :] / np.sqrt(np.sum(w[1:, :] * w[1:, :], axis=1)).reshape((-1, 1))
    word_embedding_matrix = torch.tensor(w, requires_grad=True)
    p1 = np.random.uniform(low=-1, high=1, size=[101, 5])
    p1 /= np.sqrt(np.sum(p1 * p1, axis=1)).reshape((-1, 1))
    position1_embedding_matrix = torch.tensor(np.vstack([np.zeros((1, 5)), p1]), requires_grad=True).float()
    p2 = np.random.uniform(low=-1, high=1, size=[101, 5])
    p2 /= np.sqrt(np.sum(p2 * p2, axis=1)).reshape((-1, 1))
    position2_embedding_matrix = torch.tensor(np.vstack([np.zeros((1, 5)), p2]), requires_grad=True).float()
    word_dim = word_embedding_matrix.shape[1]
    pos_dim = position1_embedding_matrix.shape[1]
    logger("Load train and test data.")
    train_bags = load_bags(os.path.join(args.data_path, "train_bags.txt"))
    test_bags = load_bags(os.path.join(args.data_path, "test_bags.txt"))
    logger("Load model.")
    model = APCNN_NIS(args.class_num, word_embedding_matrix, position1_embedding_matrix, position2_embedding_matrix, \
        args.filters, (args.kernel_size, word_dim + 2 * pos_dim), padding=(int(args.kernel_size/2), 0), \
        nis_hidden_dims=nis_hidden_dims).to(device)
    loss_function = nn.CrossEntropyLoss().to(device)
    optimizer = optim.Adadelta(model.parameters(), rho=0.95, eps=1e-6)
    # scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=int(args.epochs/2), gamma=0.1)
    logger("Init model.")
    model.init_params()
    logger("Begin training.")
    for epoch in range(args.epochs):
        # train
        # scheduler.step()
        np.random.shuffle(train_bags)
        train_batches = split_batch(train_bags, batch_size=args.batch_size)
        for i, batch in enumerate(train_batches):
            label = [bag.relation for bag in batch]
            pred_list = []
            for bag in batch:
                train_entity_pair = [dictionary.get(bag.entity_pair[0], 0), dictionary.get(bag.entity_pair[1], 0)]
                train_sentences = np.array(bag.sentences, dtype=np.int32).reshape((bag.instance_count, args.seq_len+2*int(args.kernel_size/2)))
                train_position1 = np.array([bag.positions[m][0] for m in range(bag.instance_count)], dtype=np.int32).reshape((bag.instance_count, args.seq_len+2*int(args.kernel_size/2)))
                train_position2 = np.array([bag.positions[m][1] for m in range(bag.instance_count)], dtype=np.int32).reshape((bag.instance_count, args.seq_len+2*int(args.kernel_size/2)))
                train_entity_pos = np.array(bag.entity_pos, dtype=np.int32).reshape((bag.instance_count, 2))
                tmp_pred = model(train_sentences, train_position1, train_position2, train_entity_pos, train_entity_pair)
                pred_list.append(tmp_pred)
            pred = torch.cat(pred_list, dim=0)
            label = torch.LongTensor(label).to(device)
            loss = loss_function(pred, label)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            if i % 500 == 499:
                logger("batch = %d / %d, loss = %f" % (i+1, len(train_batches), loss.item()))
        # test
        test_relations = [bag.relation for bag in test_bags]
        test_counts = [bag.instance_count for bag in test_bags]
        test_sents = [bag.sentences for bag in test_bags]
        test_poss = [bag.positions for bag in test_bags]
        test_eposs = [bag.entity_pos for bag in test_bags]
        test_epair = [[dictionary.get(bag.entity_pair[0], 0), dictionary.get(bag.entity_pair[1], 0)] for bag in test_bags]
        predict_y, predict_y_prob, y_given = predict(model, test_relations, test_counts, test_sents, test_poss, test_eposs, test_epair, args.seq_len+2*int(args.kernel_size/2))
        test_p, test_r, _ = eval_pr(predict_y, predict_y_prob, y_given)
        logger("Epoch %d, test precision: %f; test recall: %f" % (epoch+1, test_p[-1], test_r[-1]))
        logger("Epoch %d, save pr and model." % (epoch+1))
        save_pr(prfile_path, epoch, test_p, test_r)
        torch.save(model.state_dict(), os.path.join(snapshot_path, "snapshot_%d.model" % epoch))