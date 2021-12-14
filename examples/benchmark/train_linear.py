import argparse
import json
import numbers
import os
import time
from typing import List, Dict

import dask
import dask.dataframe as dd
import numpy as np
import ray
import torch
from dask_ml.preprocessing import StandardScaler
from ray import train
from ray.train import Trainer
from ray.train.callbacks import JsonLoggerCallback
from ray.util.dask import ray_dask_get
from ray.util.ml_utils.json import SafeFallbackEncoder
from torch import nn

from ray_sklearn.models.tabnet import TabNet

ray.data.set_progress_bars(False)

def max_and_argmax(val):
    return np.max(val), np.argmax(val)


def min_and_argmin(val):
    return np.min(val), np.argmin(val)


DEFAULT_AGGREGATE_FUNC = {
    "mean": np.mean,
    "median": np.median,
    "std": np.std,
    "max": max_and_argmax,
    "min": min_and_argmin
}

DEFAULT_KEYS_TO_IGNORE = {
    "epoch", "_timestamp", "_training_iteration", "train_batch_size",
    "valid_batch_size", "lr"
}

class AggregateLogCallback(JsonLoggerCallback):
    def handle_result(self, results: List[Dict], **info):
        results_dict = {idx: val for idx, val in enumerate(results)}

        aggregate_results = {}
        for key, value in results_dict[0].items():
            if key in DEFAULT_KEYS_TO_IGNORE:
                aggregate_results[key] = value
            elif isinstance(value, numbers.Number):
                aggregate_key = [
                    result[key] for result in results if key in result
                ]

                aggregate = {}
                for func_key, func in DEFAULT_AGGREGATE_FUNC.items():
                    aggregate[func_key] = func(aggregate_key)
                aggregate_results[key] = aggregate


        final_results = {}
        final_results["raw"] = results_dict
        final_results["aggregated"] = aggregate_results

        with open(self._log_path, "r+") as f:
            loaded_results = json.load(f)
            f.seek(0)
            json.dump(
                loaded_results + [final_results], f, cls=SafeFallbackEncoder)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--num-workers",
        type=int,
        default=4,
        help="Number of workers.",
    )
    parser.add_argument(
        "--use-gpu",
        action="store_true",
        help="If enabled, GPU will be used.",
    )
    parser.add_argument(
        "--address",
        type=str,
        required=False,
        help="The Ray address to use.",
    )
    parser.add_argument(
        "--num-epochs",
        type=int,
        default=5,
        help="Sets the number of training epochs. Defaults to 5.",
    )
    parser.add_argument(
        "--fraction",
        type=float,
        default=1.0,
        help="Fraction of data to train on",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1024,
        help="The mini-batch size. Each worker will process "
             "batch-size/num-workers records at a time. Defaults to 1024.",
    )
    parser.add_argument(
        "--worker-batch-size",
        type=int,
        required=False,
        help="The per-worker batch size. If set this will take precedence the batch-size argument.",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="If enabled, training data will be globally shuffled.",
    )
    parser.add_argument(
        "--lr",
        type=float,
        default=0.02,
        help="The learning rate. Defaults to 0.02",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="If enabled, debug logs will be printed.",
    )

    args = parser.parse_args()
    num_workers = args.num_workers
    use_gpu = args.use_gpu
    address = args.address
    num_epochs = args.num_epochs
    fraction = args.fraction
    batch_size = args.batch_size
    worker_batch_size = args.worker_batch_size
    shuffle = args.shuffle
    lr = args.lr
    debug = args.debug

    target = "fare_amount"
    features = [
        "pickup_longitude", "pickup_latitude", "dropoff_longitude",
        "dropoff_latitude", "passenger_count"
    ]

    ray.init(address=address)

    from sklearn.datasets import make_regression
    def data_creator(rows, cols):
        X_regr, y_regr = make_regression(
            rows, cols, n_informative=cols // 2, random_state=0)
        X_regr = X_regr.astype(np.float32)
        y_regr = y_regr.astype(np.float32) / 100
        y_regr = y_regr.reshape(-1, 1)
        return (X_regr, y_regr)


    import torch.nn.functional as F
    class RegressorModule(nn.Module):
        def __init__(
                self,
                input_dim,
                output_dim,
                num_units=10,
                nonlin=F.relu,
        ):
            super().__init__()
            self.num_units = num_units
            self.nonlin = nonlin

            self.dense0 = nn.Linear(input_dim, num_units)
            self.nonlin = nonlin
            self.dense1 = nn.Linear(num_units, 10)
            self.output = nn.Linear(10, output_dim)

        def forward(self, X: torch.Tensor, **kwargs):
            X = self.nonlin(self.dense0(X))
            X = F.relu(self.dense1(X))
            X = self.output(X)
            return X


    def train_func(config):

        # model = RegressorModule(input_dim=20, output_dim=1)

        model = TabNet(input_dim=20, output_dim=1)

        model = train.torch.prepare_model(model)


        criterion = nn.MSELoss()
        optimizer = torch.optim.Adam(model.parameters(), lr=lr)

        device = train.torch.get_device()

        results = []
        for i in range(num_epochs):
            # train_dataset = next(train_dataset_iterator)
            # train_torch_dataset = train_dataset.to_torch(
            #     label_column=target, batch_size=train_worker_batch_size)

            X, y = data_creator(2048, 20)
            #
            # X = pd.DataFrame(X)
            # y = pd.Series(y.ravel())
            # y.name = "target"

            dataset = torch.utils.data.TensorDataset(torch.Tensor(X), torch.Tensor(y))
            dataloader = torch.utils.data.DataLoader(dataset, batch_size=64)

            model.train()
            train_train_loss = 0
            train_num_rows = 0
            for batch_idx, (X, y) in enumerate(dataloader):

                X = X.to(device)
                y = y.to(device)
                pred = model(X)
                loss = criterion(pred, y)
                optimizer.zero_grad()
                loss.backward()

                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()

                count = len(X)
                train_train_loss += count * loss.item()
                train_num_rows += count

            train_loss = train_train_loss / train_num_rows  # TODO: should this be num batches or num rows?
            print(f"Train epoch: [{i}], mean square error:[{train_loss}]")

            # scheduler.step(train_loss)
            curr_lr = [ group['lr'] for group in optimizer.param_groups ]

            train.report(train_mse=train_loss, lr=curr_lr)

            state_dict = model.state_dict()
            from torch.nn.modules.utils import \
                consume_prefix_in_state_dict_if_present
            consume_prefix_in_state_dict_if_present(state_dict, "module.")
            train.save_checkpoint(model_state_dict=state_dict)
        return results

    train_start = time.time()

    trainer = Trainer("torch", num_workers=num_workers, use_gpu=use_gpu)
    trainer.start()
    # results = trainer.run(train_func, dataset=datasets, callbacks=[AggregateLogCallback()])
    results = trainer.run(train_func, callbacks=[AggregateLogCallback()])
    trainer.shutdown()


    train_end = time.time()
    train_time = train_end - train_start

    print(f"Training completed in {train_time} seconds.")


    print("Done!")
    print(results)