import os
from typing import Callable, Union

import numpy as np
import torch
import trimesh
from tqdm import tqdm

from .body_models import BodyModel, get_model_params
from .config import CfgNode, get_cfg
from .data import get_dataset
from .losses import (compute_edge_loss, compute_v2v_error,
                     compute_vertex_loss)
from .utils import (deform_vertices, read_deformation_matrix,
                    seed_everything, validate_device)
from typing import Mapping


class Engine:
    """
    Main worker class to either fit body models or to convert
    between different types of body models.
    """

    def __init__(
            self,
            cfg: Union[str, CfgNode],
    ):

        if isinstance(cfg, str):
            self.cfg = get_cfg(cfg)
        else:
            self.cfg = cfg

        self._setup()

        os.makedirs(self.cfg.output.save_dir, exist_ok=True)
        os.makedirs(os.path.join(self.cfg.output.save_dir, "params"), exist_ok=True)
        if self.cfg.output.save_meshes is True:
            os.makedirs(os.path.join(self.cfg.output.save_dir, "meshes"), exist_ok=True)

    def _setup(self):

        seed_everything(self.cfg.seed)

        self._setup_device()

        self.input_body_model = None
        self.setup_dataloader()

        self._setup_output_body_model()

        self.params_info = BodyModel.body_models[self.cfg.output.body_model.type].get_body_model_params_info()
        self.output_body_model_params = {}
        # for params_name in self.params_info.keys():
        # self.output_body_model_params["betas"] = None
        #     self.output_body_model_params[params_name] = None
        self._init_params()

        self._setup_deformation()
        self._setup_vertex_masking()

    def _setup_device(self):

        self.device = validate_device(self.cfg.device)

    def _setup_deformation(
            self,
    ):
        self.deformation_matrix = None
        if self.cfg.deformation_matrix_path is not None:
            self.deformation_matrix = read_deformation_matrix(
                self.cfg.deformation_matrix_path, self.device
            )

    def _setup_vertex_masking(
            self,
    ):
        self.vertices_mask = None
        if self.cfg.vertices_mask_path is not None:
            self.vertices_mask = np.load(self.cfg.vertices_mask_path)

    def _load_betas(self, source=False):
        fname = self.cfg.input.source_betas_override_path if source else self.cfg.output.target_betas_path
        if fname is None: return None
        data = np.load(fname, allow_pickle=True)
        betas = data['betas'] if isinstance(data, Mapping) else data
        return torch.as_tensor(betas, dtype=torch.float32, device=self.device)

    def _load_vtemplate(self, source=False):
        fname = self.cfg.input.source_vtemplate_path if source else self.cfg.output.target_vtemplate_path
        if fname is None: return None
        print(f"Loading {'source' if source else 'target'} vtemplate from {fname}")
        with open(fname, 'rb') as f:
            ext = fname.split('.')[-1]
            v_template = np.asarray(trimesh.load(f, file_type=ext, process=False).vertices).astype(np.float32)
        return v_template

    def _load_pose(self):
        fname = self.cfg.output.target_pose_path
        if fname is None: return None
        data = np.load(fname, allow_pickle=True)
        pose = data['betas'] if isinstance(data, Mapping) else data
        return torch.as_tensor(pose, dtype=torch.float32, device=self.device)

    def _init_params(
            self,
            inherit_prev_betas_without_grad: bool = False,
    ):

        disable_params_optimization = []
        if self.cfg.output.target_betas_path is not None or self.cfg.output.target_vtemplate_path is not None:
            if self.cfg.output.target_betas_path is not None:
                print(f"Loaded target betas from {self.cfg.output.target_betas_path}")
                self.output_body_model_params["betas"] = self._load_betas(source=False)
            if self.cfg.output.target_vtemplate_path is not None:
                print(f"Loaded target v_template from {self.cfg.output.target_vtemplate_path}")
                self.output_body_model_params["betas"] = torch.zeros(
                    (1, self.cfg.output.body_model.n_betas),
                    dtype=torch.float32,
                    device=self.device,
                    requires_grad=True,
                )
            inherit_prev_betas_without_grad = True
            self.cfg.output.single_set_of_betas_per_batch = True
        else:
            if self.cfg.output.single_set_of_betas_per_batch is True:
                self.output_body_model_params["betas"] = torch.zeros(
                    (1, self.cfg.output.body_model.n_betas),
                    dtype=torch.float32,
                    device=self.device,
                    requires_grad=True,
                )
            else:
                self.output_body_model_params["betas"] = torch.zeros(
                    (self.cfg.batch_size, self.cfg.output.body_model.n_betas),
                    dtype=torch.float32,
                    device=self.device,
                    requires_grad=True,
                )

        if self.cfg.output.target_pose_path is not None:
            target_pose = self._load_pose()
            self.output_body_model_params["global_orient"] = target_pose[:, :3]
            self.output_body_model_params["pose"] = target_pose[:, 3:]
            disable_params_optimization = ['global_orient', 'pose']

        if inherit_prev_betas_without_grad is True:
            assert (
                    self.output_body_model_params["betas"] is not None
            ), "Betas must be optimized at least once"

            self.output_body_model_params["betas"] = (
                self.output_body_model_params["betas"]
                .clone()
                .detach()
                .requires_grad_(False)
            )

        for params_name, params_size in self.params_info.items():
            if params_name in disable_params_optimization: continue
            self.output_body_model_params[params_name] = torch.zeros(
                (self.cfg.batch_size, params_size),
                dtype=torch.float32,
                device=self.device,
                requires_grad=True,
            )

        params_to_optimize = self.cfg.output.params_to_optimize
        if params_to_optimize is not None and params_to_optimize != "all":

            assert isinstance(
                params_to_optimize, (tuple, list)
            ), "params_to_optimize must be a list"
            for params_name in params_to_optimize:
                assert isinstance(
                    params_name, str
                ), "params_to_optimize must be a list of strings"
                assert (
                        params_name in self.output_body_model_params.keys()
                ), f"{params_name} is not a valid parameter name"

            for params_name in self.output_body_model_params.keys():
                if params_name not in params_to_optimize:
                    self.output_body_model_params[params_name].requires_grad_(False)

    def _setup_input_body_model(
            self,
    ):

        if self.cfg.input.body_model.misc_args is None:
            misc_args = {}
        else:
            misc_args = self.cfg.input.body_model.misc_args.to_dict()

        self.input_body_model = BodyModel.instantiate(model_type=self.cfg.input.body_model.type,
                                                      # kwargs
                                                      model_path=self.cfg.input.body_model.path,
                                                      gender=self.cfg.input.body_model.gender,
                                                      n_betas=self.cfg.input.body_model.n_betas,
                                                      batch_size=self.cfg.batch_size,
                                                      v_template=self._load_vtemplate(source=True),
                                                      device=self.device,
                                                      misc_args=misc_args).eval()

    def _setup_output_body_model(
            self,
    ):

        if self.cfg.output.body_model.misc_args is None:
            misc_args = {}
        else:
            misc_args = self.cfg.output.body_model.misc_args.to_dict()

        self.output_body_model = BodyModel.instantiate(model_type=self.cfg.output.body_model.type,
                                                       # kwargs
                                                       model_path=self.cfg.output.body_model.path,
                                                       gender=self.cfg.output.body_model.gender,
                                                       n_betas=self.cfg.output.body_model.n_betas,
                                                       batch_size=self.cfg.batch_size,
                                                       v_template=self._load_vtemplate(source=False),
                                                       device=self.device,
                                                       misc_args=misc_args)

    def setup_dataloader(
            self,
    ):

        dataset_info = get_dataset(
            input_data_type=self.cfg.input.data.type,
            dataloader_batch_size=self.cfg.batch_size,
        )
        input_data_type = dataset_info["input_data_type"]
        dataset_class = dataset_info["dataset"]
        dataloader_batch_size = dataset_info["dataloader_batch_size"]

        if input_data_type == "meshes":
            self.dataset = dataset_class(
                mesh_dir=self.cfg.input.data.mesh_dir,
                mesh_format=self.cfg.input.data.mesh_format,
            )

        elif input_data_type == "params":
            if self.input_body_model is None:
                self._setup_input_body_model()

            source_betas_override = self._load_betas(source=True)

            self.dataset = dataset_class(
                body_model=self.input_body_model,
                # body_model_type=self.cfg.input.body_model.type,
                body_model_batch_size=self.cfg.batch_size,
                npz_files_dir=self.cfg.input.data.npz_files_dir,
                n_betas=self.cfg.input.body_model.n_betas,
                betas_override=source_betas_override,
                device=self.device,
            )

        self.dataloader = torch.utils.data.DataLoader(
            self.dataset,
            batch_size=dataloader_batch_size,
            shuffle=False,
            num_workers=self.cfg.dataloader_n_workers,
        )

    def _get_params_to_optimize_for_optimization_stage(self, optimization_stage: str):

        assert optimization_stage in (
            "edge_loss",
            "global_position",
            "vertex_loss",
        ), "optimization_stage must be one of ('edge_loss', 'global_position', 'vertex_loss')"

        body_model_param_names = list(self.output_body_model_params.keys())

        if optimization_stage == "edge_loss":
            param_names_to_optimize_for_optimization_stage = [
                param_name
                for param_name in body_model_param_names
                if "pose" in param_name or "orient" in param_name
            ]

        elif optimization_stage == "global_position":
            param_names_to_optimize_for_optimization_stage = [
                param_name
                for param_name in body_model_param_names
                if "trans" in param_name or "orient" in param_name
            ]

        elif optimization_stage == "vertex_loss":
            param_names_to_optimize_for_optimization_stage = body_model_param_names

        return param_names_to_optimize_for_optimization_stage

    def _setup_optimizer(self, optimization_stage: str):

        param_names_to_optimize_for_optimization_stage = (
            self._get_params_to_optimize_for_optimization_stage(
                optimization_stage=optimization_stage
            )
        )

        params_to_optimize_for_optimzation_stage = []
        for param_name in param_names_to_optimize_for_optimization_stage:
            params_to_optimize_for_optimzation_stage.append(
                self.output_body_model_params[param_name]
            )

        optimizer = torch.optim.LBFGS(
            params=params_to_optimize_for_optimzation_stage,
            **self.cfg.optimization.optimizer_params.to_dict(),
        )

        return optimizer, params_to_optimize_for_optimzation_stage

    def _compute_loss(
            self,
            n_iter: int,
            estimated_vertices: torch.Tensor,
            target_vertices: torch.Tensor,
            loss_fn: Callable,
            loss_fn_kwargs: dict = None,
            params_regularization_weights: Union[tuple, list] = None,
            params_regularization_iters: Union[tuple, list] = None,
    ):

        if loss_fn_kwargs is None: loss_fn_kwargs = {}
        loss = loss_fn(estimated_vertices, target_vertices, **loss_fn_kwargs)

        if params_regularization_weights is not None:
            assert isinstance(
                params_regularization_weights, dict
            ), "params_regularization_weights must be a dictionary containing (param_name, weight) pairs"
            assert isinstance(
                params_regularization_iters, dict
            ), "params_regularization_iters must be a dictionary containing (param_name, iter) pairs"

            for (
                    param_name,
                    regularization_weight,
            ) in params_regularization_weights.items():
                if n_iter < params_regularization_iters[param_name]:
                    loss += regularization_weight * torch.mean(
                        self.output_body_model_params[param_name] ** 2
                    )

        return loss

    def _optimize(
            self,
            optimization_stage: str,
            n_iters: int,
            target_vertices: torch.Tensor,
            loss_fn: Callable,
            loss_fn_kwargs: dict = None,
            apply_rotation_angles_correction: bool = False,
            low_loss_threshold: float = 2e-3,
            low_loss_delta_threshold: float = 1e-6,
            n_consecutive_low_loss_delta_iters_threshold: int = 5,
            gradient_clip: float = None,
            params_regularization_weights: Union[tuple, list] = None,
            params_regularization_iters: Union[tuple, list] = None,
    ):

        if loss_fn_kwargs is None: loss_fn_kwargs = {}
        optimizer, params_to_optimize_for_optimization_stage = self._setup_optimizer(
            optimization_stage=optimization_stage
        )

        prev_loss = 1e10
        n_consecutive_low_loss_delta_iters = 0
        low_loss_delta_hit_iter_idx = 0
        last_k_losses = []

        for n_iter in tqdm(range(n_iters), desc="Optimizing..."):

            def closure():
                optimizer.zero_grad()

                betas, pose, trans = get_model_params(self.output_body_model, self.output_body_model_params)
                estimated_vertices = self.output_body_model.forward(betas=betas, pose=pose, trans=trans)

                loss = self._compute_loss(
                    n_iter=n_iter,
                    estimated_vertices=estimated_vertices,
                    target_vertices=target_vertices,
                    loss_fn=loss_fn,
                    loss_fn_kwargs=loss_fn_kwargs,
                    params_regularization_weights=params_regularization_weights,
                    params_regularization_iters=params_regularization_iters,
                )

                loss.backward()

                if gradient_clip is not None:
                    torch.nn.utils.clip_grad_norm_(
                        params_to_optimize_for_optimization_stage, gradient_clip
                    )

                return loss

            loss = optimizer.step(closure)

            if apply_rotation_angles_correction is True:
                with torch.no_grad():
                    for param_name in self.output_body_model_params.keys():
                        if "pose" in param_name or "orient" in param_name:
                            self.output_body_model_params[param_name][:] = torch.atan2(
                                self.output_body_model_params[param_name].sin(),
                                self.output_body_model_params[param_name].cos(),
                            )

            if n_iter % self.cfg.log_iterations_interval == 0:
                print(f"Iteration {n_iter + 1}/{n_iters} | Loss: {loss.item():.8f}")

            if loss.item() < low_loss_threshold:
                print(
                    f"Loss threshold ({low_loss_threshold}) reached at iteration {n_iter + 1}. Stopping optimization."
                )
                print(
                    f"Last {n_consecutive_low_loss_delta_iters_threshold} losses: {last_k_losses[1:]}"
                )
                break

            if abs(loss.item() - prev_loss) < low_loss_delta_threshold:
                if n_consecutive_low_loss_delta_iters == 0:
                    n_consecutive_low_loss_delta_iters += 1
                else:
                    if n_iter - low_loss_delta_hit_iter_idx == 1:
                        n_consecutive_low_loss_delta_iters += 1
                    else:
                        n_consecutive_low_loss_delta_iters = 1

                low_loss_delta_hit_iter_idx = n_iter

            if n_iter >= n_consecutive_low_loss_delta_iters_threshold + 1:
                last_k_losses.pop(0)
            last_k_losses.append(loss.item())

            if (
                    n_consecutive_low_loss_delta_iters
                    >= n_consecutive_low_loss_delta_iters_threshold
            ):
                print(
                    f"Low loss delta threshold ({low_loss_delta_threshold}) for {n_consecutive_low_loss_delta_iters_threshold} consecutive iterations reached at iteration {n_iter + 1}. Stopping optimization."
                )
                print(
                    f"Last {n_consecutive_low_loss_delta_iters_threshold} losses: {last_k_losses[1:]}"
                )
                print(
                    f"Last {n_consecutive_low_loss_delta_iters_threshold} loss deltas: {[last_k_losses[i] - last_k_losses[i + 1] for i in range(n_consecutive_low_loss_delta_iters_threshold)]}"
                )
                print(f"Final loss: {loss.item()}")
                break

            prev_loss = loss.item()

    def _save_results(self, n_batch: int, output_vertices: torch.Tensor = None):

        save_output_body_model_params = {}
        for param_name, param_value in self.output_body_model_params.items():
            save_output_body_model_params[param_name] = (
                param_value.detach().cpu().numpy()
            )

        np.savez(
            os.path.join(self.cfg.output.save_dir, "params", f"batch_{n_batch}.npz"),
            **save_output_body_model_params,
        )

        if self.cfg.output.save_meshes is True:
            assert (
                    output_vertices is not None
            ), "If output meshes are to be saved, output_vertices must be provided"

            output_vertices = output_vertices.detach().cpu().numpy()
            faces = self.output_body_model.faces.detach().cpu().numpy()

            batch_meshes_save_dir = os.path.join(
                self.cfg.output.save_dir, "meshes", f"batch_{n_batch}"
            )
            os.makedirs(batch_meshes_save_dir, exist_ok=True)

            if output_vertices.ndim == 2:
                output_vertices = output_vertices.unsqueeze(0)

            if output_vertices.ndim == 4:
                output_vertices = output_vertices.squeeze(0)

            for n_sample in range(output_vertices.shape[0]):
                output_mesh = trimesh.Trimesh(
                    vertices=output_vertices[n_sample],
                    faces=faces,
                    process=False,
                )
                output_mesh.export(
                    os.path.join(batch_meshes_save_dir, str(n_sample).zfill(6) + ".obj")
                )

    def run(
            self,
    ):

        for n_batch, input_data in enumerate(self.dataloader):

            if n_batch == 0:
                self._init_params()

            else:
                if self.cfg.output.optimize_betas_only_for_first_batch is True:
                    self._init_params(inherit_prev_betas_without_grad=True)
                else:
                    self._init_params()

            print(f"Processing batch {n_batch + 1}/{len(self.dataloader)}")

            target_vertices = input_data["vertices"].to(self.device)
            if target_vertices.ndim == 4:
                target_vertices = target_vertices.squeeze(0)
            if target_vertices.ndim == 2:
                target_vertices = target_vertices.unsqueeze(0)

            if target_vertices.shape[0] != self.cfg.batch_size:
                raise ValueError(
                    f"Batch size of input data ({target_vertices.shape[0]}) does not match batch size specified in config ({self.cfg.batch_size})"
                )

            if self.deformation_matrix is not None:
                target_vertices = deform_vertices(
                    deformation_matrix=self.deformation_matrix,
                    vertices=target_vertices,
                )

            if self.cfg.optimization.edge_loss.use is True:
                print("\nPerforming pose optimization using an edge loss\n")
                self._optimize(
                    optimization_stage="edge_loss",
                    n_iters=self.cfg.optimization.edge_loss.n_iters,
                    target_vertices=target_vertices,
                    loss_fn=compute_edge_loss,
                    loss_fn_kwargs={
                        "faces": self.output_body_model.faces,
                        "vertices_mask": self.vertices_mask,
                        "reduction": self.cfg.optimization.edge_loss.loss_reduction,
                    },
                    apply_rotation_angles_correction=self.cfg.optimization.edge_loss.apply_rotation_angles_correction,
                    low_loss_threshold=self.cfg.optimization.edge_loss.low_loss_threshold,
                    low_loss_delta_threshold=self.cfg.optimization.edge_loss.low_loss_delta_threshold,
                    n_consecutive_low_loss_delta_iters_threshold=self.cfg.optimization.edge_loss.n_consecutive_low_loss_delta_iters_threshold,
                    gradient_clip=self.cfg.optimization.edge_loss.gradient_clip,
                    params_regularization_weights=self.cfg.optimization.edge_loss.params_regularization_weights.to_dict(),
                    params_regularization_iters=self.cfg.optimization.edge_loss.params_regularization_iters.to_dict(),
                )

            if self.cfg.optimization.global_position.use is True:
                print(
                    "\nPerforming global translation and orientation optimization using a vertex loss\n"
                )
                self._optimize(
                    optimization_stage="global_position",
                    n_iters=self.cfg.optimization.global_position.n_iters,
                    target_vertices=target_vertices,
                    loss_fn=compute_vertex_loss,
                    loss_fn_kwargs={
                        "reduction": self.cfg.optimization.global_position.loss_reduction,
                    },
                    apply_rotation_angles_correction=self.cfg.optimization.global_position.apply_rotation_angles_correction,
                    low_loss_threshold=self.cfg.optimization.global_position.low_loss_threshold,
                    low_loss_delta_threshold=self.cfg.optimization.global_position.low_loss_delta_threshold,
                    n_consecutive_low_loss_delta_iters_threshold=self.cfg.optimization.global_position.n_consecutive_low_loss_delta_iters_threshold,
                    gradient_clip=self.cfg.optimization.global_position.gradient_clip,
                    params_regularization_weights=self.cfg.optimization.global_position.params_regularization_weights.to_dict(),
                    params_regularization_iters=self.cfg.optimization.global_position.params_regularization_iters.to_dict(),
                )

            print("\nOptimizing all parameters using a vertex loss\n")
            self._optimize(
                optimization_stage="vertex_loss",
                n_iters=self.cfg.optimization.vertex_loss.n_iters,
                target_vertices=target_vertices,
                loss_fn=compute_vertex_loss,
                loss_fn_kwargs={
                    "reduction": self.cfg.optimization.vertex_loss.loss_reduction,
                    "vertices_mask": self.vertices_mask,
                },
                apply_rotation_angles_correction=self.cfg.optimization.vertex_loss.apply_rotation_angles_correction,
                low_loss_threshold=self.cfg.optimization.vertex_loss.low_loss_threshold,
                low_loss_delta_threshold=self.cfg.optimization.vertex_loss.low_loss_delta_threshold,
                n_consecutive_low_loss_delta_iters_threshold=self.cfg.optimization.vertex_loss.n_consecutive_low_loss_delta_iters_threshold,
                gradient_clip=self.cfg.optimization.vertex_loss.gradient_clip,
                params_regularization_weights=self.cfg.optimization.vertex_loss.params_regularization_weights.to_dict(),
                params_regularization_iters=self.cfg.optimization.vertex_loss.params_regularization_iters.to_dict(),
            )

            # final_estimated_vertices = perform_model_forward_pass(
            #     body_model_type=self.cfg.output.body_model.type,
            #     body_model=self.output_body_model,
            #     params=self.output_body_model_params,
            #     n_betas=self.cfg.output.body_model.n_betas,
            #     batch_size=self.cfg.batch_size,
            #     device=self.device,
            # )

            betas, pose, trans = get_model_params(self.output_body_model, self.output_body_model_params)
            final_estimated_vertices = self.output_body_model.forward(betas=betas, pose=pose, trans=trans)

            final_v2v_error = compute_v2v_error(
                final_estimated_vertices, target_vertices
            )

            print(
                f"\nOptimization complete. Final v2v error: {final_v2v_error * 1000} mm"
            )
            print("\nSaving results\n")

            self._save_results(
                n_batch=n_batch,
                output_vertices=final_estimated_vertices,
            )
