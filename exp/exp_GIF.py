from cgi import test
import logging
import time
import os

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import grad
import networkx as nx
from sklearn.model_selection import train_test_split
from sklearn.metrics import f1_score

torch.cuda.empty_cache()
import torch_geometric.transforms as T
from torch_geometric.datasets import Planetoid
from torch_geometric.data import NeighborSampler
from torch_geometric.nn.conv.gcn_conv import gcn_norm
import numpy as np

from exp.exp import Exp
from lib_gnn_model.gat.gat_net_batch import GATNet
from lib_gnn_model.gin.gin_net_batch import GINNet
from lib_gnn_model.gcn.gcn_net_batch import GCNNet
# from lib_gnn_model.graphsage.graphsage_net import SageNet
from lib_gnn_model.sgc.sgc_net_batch import SGCNet
from lib_gnn_model.node_classifier import NodeClassifier
from lib_gnn_model.gnn_base import GNNBase
from parameter_parser import parameter_parser
from lib_utils import utils


class ExpGraphInfluenceFunction(Exp):
    def __init__(self, args):
        super(ExpGraphInfluenceFunction, self).__init__(args)

        self.logger = logging.getLogger('ExpGraphInfluenceFunction')
        self.deleted_nodes = np.array([])     
        self.feature_nodes = np.array([])
        self.influence_nodes = np.array([])

        self.load_data()
        self.num_feats = self.data.num_features
        self.train_test_split()
        self.unlearning_request()

        self.target_model_name = self.args['target_model']

        # self.get_edge_indeces()
        self.determine_target_model()

        run_f1 = np.empty((0))
        run_f1_unlearning = np.empty((0))
        unlearning_times = np.empty((0))
        training_times = np.empty((0))
        for run in range(self.args['num_runs']):
            self.logger.info("Run %f" % run)

            run_training_time, result_tuple = self._train_model(run)

            f1_score = self.evaluate(run)
            run_f1 = np.append(run_f1, f1_score)
            training_times = np.append(training_times, run_training_time)

            # unlearning with GIF
            unlearning_time, f1_score_unlearning = self.gif_approxi(result_tuple)
            unlearning_times = np.append(unlearning_times, unlearning_time)
            run_f1_unlearning = np.append(run_f1_unlearning, f1_score_unlearning)

        f1_score_avg = np.average(run_f1)
        f1_score_std = np.std(run_f1)
        self.logger.info("f1_score: avg=%s, std=%s" % (f1_score_avg, f1_score_std))
        self.logger.info("model training time: avg=%s seconds" % np.average(training_times))

        f1_score_unlearning_avg = np.average(run_f1_unlearning)
        f1_score_unlearning_std = np.std(run_f1_unlearning)
        unlearning_time_avg = np.average(unlearning_times)
        self.logger.info("f1_score of GIF: avg=%s, std=%s" % (f1_score_unlearning_avg, f1_score_unlearning_std))
        self.logger.info("GIF unlearing time: avg=%s seconds" % np.average(unlearning_time_avg))

    def load_data(self):
        self.data = self.data_store.load_raw_data()

    def train_test_split(self):
        if self.args['is_split']:
            self.logger.info('splitting train/test data')
            # use the dataset's default split
            if self.data.name in ['ogbn-arxiv', 'ogbn-products']:
                self.train_indices, self.test_indices = self.data.train_indices.numpy(), self.data.test_indices.numpy()
            else:
                self.train_indices, self.test_indices = train_test_split(np.arange((self.data.num_nodes)), test_size=self.args['test_ratio'], random_state=100)
                
            self.data_store.save_train_test_split(self.train_indices, self.test_indices)

            self.data.train_mask = torch.from_numpy(np.isin(np.arange(self.data.num_nodes), self.train_indices))
            self.data.test_mask = torch.from_numpy(np.isin(np.arange(self.data.num_nodes), self.test_indices))
        else:
            self.train_indices, self.test_indices = self.data_store.load_train_test_split()

            self.data.train_mask = torch.from_numpy(np.isin(np.arange(self.data.num_nodes), self.train_indices))
            self.data.test_mask = torch.from_numpy(np.isin(np.arange(self.data.num_nodes), self.test_indices))

    def unlearning_request(self):
        self.logger.debug("Train data  #.Nodes: %f, #.Edges: %f" % (
            self.data.num_nodes, self.data.num_edges))

        self.data.x_unlearn = self.data.x.clone()
        self.data.edge_index_unlearn = self.data.edge_index.clone()
        edge_index = self.data.edge_index.numpy()
        unique_indices = np.where(edge_index[0] < edge_index[1])[0]

        if self.args["unlearn_task"] == 'node':
            unique_nodes = np.random.choice(len(self.train_indices),
                                            int(len(self.train_indices) * self.args['unlearn_ratio']),
                                            replace=False)
            self.data.edge_index_unlearn = self.update_edge_index_unlearn(unique_nodes)

        if self.args["unlearn_task"] == 'edge':
            remove_indices = np.random.choice(
                unique_indices,
                int(unique_indices.shape[0] * self.args['unlearn_ratio']),
                replace=False)
            remove_edges = edge_index[:, remove_indices]
            unique_nodes = np.unique(remove_edges)
        
            self.data.edge_index_unlearn = self.update_edge_index_unlearn(unique_nodes, remove_indices)

        if self.args["unlearn_task"] == 'feature':
            unique_nodes = np.random.choice(len(self.train_indices),
                                            int(len(self.train_indices) * self.args['unlearn_ratio']),
                                            replace=False)
            self.data.x_unlearn[unique_nodes] = 0.
        self.find_k_hops(unique_nodes)

    def update_edge_index_unlearn(self, delete_nodes, delete_edge_index=None):
        edge_index = self.data.edge_index.numpy()

        unique_indices = np.where(edge_index[0] < edge_index[1])[0]
        unique_indices_not = np.where(edge_index[0] > edge_index[1])[0]

        if self.args["unlearn_task"] == 'edge':
            remain_indices = np.setdiff1d(unique_indices, delete_edge_index)
        else:
            unique_edge_index = edge_index[:, unique_indices]
            delete_edge_indices = np.logical_or(np.isin(unique_edge_index[0], delete_nodes),
                                                np.isin(unique_edge_index[1], delete_nodes))
            remain_indices = np.logical_not(delete_edge_indices)
            remain_indices = np.where(remain_indices == True)

        remain_encode = edge_index[0, remain_indices] * edge_index.shape[1] * 2 + edge_index[1, remain_indices]
        unique_encode_not = edge_index[1, unique_indices_not] * edge_index.shape[1] * 2 + edge_index[0, unique_indices_not]
        sort_indices = np.argsort(unique_encode_not)
        remain_indices_not = unique_indices_not[sort_indices[np.searchsorted(unique_encode_not, remain_encode, sorter=sort_indices)]]
        remain_indices = np.union1d(remain_indices, remain_indices_not)

        return torch.from_numpy(edge_index[:, remain_indices])

    def determine_target_model(self):
        self.logger.info('target model: %s' % (self.args['target_model'],))
        num_classes = len(self.data.y.unique())

        self.target_model = NodeClassifier(self.num_feats, num_classes, self.args)

    def evaluate(self, run):
        self.logger.info('model evaluation')

        start_time = time.time()
        posterior = self.target_model.posterior()
        test_f1 = f1_score(
            self.data.y[self.data['test_mask']].cpu().numpy(), 
            posterior.argmax(axis=1).cpu().numpy(), 
            average="micro"
        )

        evaluate_time = time.time() - start_time
        self.logger.info("Evaluation cost %s seconds." % evaluate_time)

        self.logger.info("Final Test F1: %s" % (test_f1,))
        return test_f1

    def _train_model(self, run):
        self.logger.info('training target models, run %s' % run)

        start_time = time.time()
        self.target_model.data = self.data
        res = self.target_model.train_model(
            (self.deleted_nodes, self.feature_nodes, self.influence_nodes))
        train_time = time.time() - start_time

        # self.data_store.save_target_model(run, self.target_model)
        self.logger.info("Model training time: %s" % (train_time))

        return train_time, res
        
    def find_k_hops(self, unique_nodes):
        edge_index = self.data.edge_index.numpy()
        
        ## finding influenced neighbors
        hops = 2
        if self.args["unlearn_task"] == 'node':
            hops = 3
        influenced_nodes = unique_nodes
        for _ in range(hops):
            target_nodes_location = np.isin(edge_index[0], influenced_nodes)
            neighbor_nodes = edge_index[1, target_nodes_location]
            influenced_nodes = np.append(influenced_nodes, neighbor_nodes)
            influenced_nodes = np.unique(influenced_nodes)
        neighbor_nodes = np.setdiff1d(influenced_nodes, unique_nodes)
        if self.args["unlearn_task"] == 'feature':
            self.feature_nodes = unique_nodes
            self.influence_nodes = neighbor_nodes
        if self.args["unlearn_task"] == 'node':
            self.deleted_nodes = unique_nodes
            self.influence_nodes = neighbor_nodes
        if self.args["unlearn_task"] == 'edge':
            self.influence_nodes = influenced_nodes

    def gif_approxi(self, res_tuple):
        '''
        res_tuple == (grad_all, grad1, grad2)
        '''
        start_time = time.time()
        iteration, damp, scale = self.args['iteration'], self.args['damp'], self.args['scale']

        if self.args["method"] =="GIF":
            v = tuple(grad1 - grad2 for grad1, grad2 in zip(res_tuple[1], res_tuple[2]))
        if self.args["method"] =="IF":
            v = res_tuple[1]
        h_estimate = tuple(grad1 - grad2 for grad1, grad2 in zip(res_tuple[1], res_tuple[2]))
        for _ in range(iteration):
            model_params  = [p for p in self.target_model.model.parameters() if p.requires_grad]
            hv            = self.hvps(res_tuple[0], model_params, h_estimate)
            with torch.no_grad():
                h_estimate    = [ v1 + (1-damp)*h_estimate1 - hv1/scale
                            for v1, h_estimate1, hv1 in zip(v, h_estimate, hv)]

        # model_params = [p for p in self.target_model.model.parameters() if p.requires_grad]
        # with torch.no_grad():
        #     h_estimate = self.conjugate_gradient(res_tuple[0], model_params, h_estimate)

        # Δθ
        params_change = [h_est / scale for h_est in h_estimate]
        params_esti = [p1 + p2 for p1, p2 in zip(params_change, model_params)]


        test_F1 = self.target_model.evaluate_unlearn_F1(params_esti)
        return time.time() - start_time, test_F1

    def hvps(self, grad_all, model_params, h_estimate):
        element_product = 0
        for grad_elem, v_elem in zip(grad_all, h_estimate):
            element_product += torch.sum(grad_elem * v_elem)
        
        return_grads = grad(element_product,model_params,create_graph=True)
        return return_grads


    def conjugate_gradient(self, grad_all, model_param, v, cg_iters=10, residual_tol=1e-10):
        """
        Arguments:
            grad_all: 图G训练梯度
            model_param: 待更新参数
            v: pytorch tensor的list，代表需要与hessian矩阵逆乘积的向量
        Returns:
            x: argmin_t时的t
        """
        x = tuple(torch.zeros_like(i) for i in v)
        r = [torch.tensor(-i, requires_grad=True) for i in v]
        p = [torch.tensor(i, requires_grad=True) for i in v]

        rtr = sum(torch.sum(r_elem * r_elem) for r_elem in r)
        # 开始迭代
        for i in range(cg_iters):
            z = self.hvp(grad_all, model_param, p)
            v = rtr / sum(torch.sum(p_elem * z_elem) for p_elem, z_elem in zip(p, z))

            x = tuple(x_elem + v * p_elem for x_elem, p_elem in zip(x, p))
            r = tuple(r_elem - v * z_elem for r_elem, z_elem in zip(r, z))

            newrtr = sum(torch.sum(r_elem * r_elem) for r_elem in r)

            mu = newrtr / rtr
            p = tuple(r_elem + mu * p_elem for r_elem, p_elem in zip(r, p))
            rtr = newrtr
            if rtr < residual_tol:
                break

            print("i = {}, v = {}, mu = {}".format(i, v, mu))
            print("rtr = {}, newrtr = {}".format(rtr, newrtr))

        return x

    def hvp(self, grad_all, w, v):
        # First backprop,原先需要grad利用loss和w来求，此处直接利用结果
        first_grads = grad_all
        # Second backprop
        return_grads = grad(first_grads, w, retain_graph=True, grad_outputs=v)

        return return_grads