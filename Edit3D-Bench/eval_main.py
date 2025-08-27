#!/usr/bin/env python3
"""
Edit3D-Bench Evaluation System
Integrated evaluation system for computing multiple metrics on Edit3D-Bench dataset
"""

import argparse
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import torch

from eval_modules.data_loader import DataLoader
from eval_modules.distribution_metrics import DistributionMetricsEvaluator
from eval_modules.geometry_metrics import GeometryMetricsEvaluator

# Import evaluation modules
from eval_modules.image_metrics import ImageMetricsEvaluator
from eval_modules.text_alignment_metrics import TextAlignmentMetricsEvaluator
from eval_modules.video_metrics import VideoMetricsEvaluator

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@dataclass
class EvaluationConfig:
    """Evaluation configuration"""

    # Data paths
    gt_root: str
    pred_root: str

    # Evaluation metrics configuration
    metrics: List[str]  # List of metrics to compute

    # Device configuration
    device: str = "cuda:0"

    # Image processing configuration
    image_size: Tuple[int, int] = (512, 512)

    # Batch processing configuration
    batch_size: int = 32
    num_workers: int = 4

    # Output configuration
    output_dir: str = "evaluation_results"
    save_detailed: bool = True

    # Specific metric configuration
    max_images: Optional[int] = None  # Limit number of images to process
    max_videos: Optional[int] = None  # Limit number of videos to process

    # Ignore mask
    ignore_mask: bool = False


class EvaluationManager:
    """Evaluation manager"""

    def __init__(self, config: EvaluationConfig):
        self.config = config
        self.data_loader = DataLoader(config)

        # Remove evaluators dictionary, create on-demand instead
        # self.evaluators = {}
        # self._init_evaluators()

        # Results storage
        self.results = {}

    def _create_evaluator(self, evaluator_type: str):
        """Create evaluator of specified type"""
        if evaluator_type == "image":
            return ImageMetricsEvaluator(
                device=self.config.device,
                image_size=self.config.image_size,
                batch_size=self.config.batch_size,
                num_workers=self.config.num_workers,
                ignore_mask=self.config.ignore_mask,
            )
        elif evaluator_type == "distribution":
            return DistributionMetricsEvaluator(
                device=self.config.device,
                image_size=self.config.image_size,
                batch_size=self.config.batch_size,
                num_workers=self.config.num_workers,
            )
        elif evaluator_type == "video":
            return VideoMetricsEvaluator(
                device=self.config.device,
                batch_size=min(self.config.batch_size, 8),  # Avoid OOM
                num_workers=self.config.num_workers,
            )
        elif evaluator_type == "geometry":
            return GeometryMetricsEvaluator(
                device=self.config.device,
                batch_size=self.config.batch_size,
                num_workers=self.config.num_workers,
                ignore_mask=self.config.ignore_mask,
            )
        elif evaluator_type == "text_alignment":
            return TextAlignmentMetricsEvaluator(
                device=self.config.device,
                image_size=self.config.image_size,
                batch_size=self.config.batch_size,
                num_workers=self.config.num_workers,
            )
        else:
            raise ValueError(f"Unknown evaluator type: {evaluator_type}")

    def _cleanup_evaluator(self, evaluator):
        """Clean up evaluator and release GPU memory"""
        # Clean up various models
        model_attrs = ["model", "dino_model", "clip_model", "i3d_model"]
        for attr in model_attrs:
            if hasattr(evaluator, attr):
                delattr(evaluator, attr)

        # Delete evaluator object itself
        del evaluator
        # Force garbage collection
        import gc

        gc.collect()
        # If using CUDA, clear CUDA cache
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    @torch.no_grad()
    def run_evaluation(self) -> Dict[str, Any]:
        """Run complete evaluation"""
        logger.info("Starting evaluation...")

        # 1. Load data
        logger.info("Loading data...")
        data_pairs = self.data_loader.load_all_data()

        # 2. Define evaluation tasks and corresponding data
        evaluation_tasks = [
            ("image", data_pairs["image_pairs"], None),
            ("distribution", data_pairs["gt_images"], data_pairs["pred_images"]),
            ("video", data_pairs["gt_videos"], data_pairs["pred_videos"]),
            (
                "geometry",
                data_pairs["gt_models"],
                (data_pairs["pred_models"], data_pairs["masks"]),
            ),
            ("text_alignment", data_pairs["image_text_pairs"], None),
        ]

        # 3. Run evaluators one by one
        for evaluator_type, data1, data2 in evaluation_tasks:
            # Check if this type of evaluation is needed
            evaluator_class_map = {
                "image": ImageMetricsEvaluator,
                "distribution": DistributionMetricsEvaluator,
                "video": VideoMetricsEvaluator,
                "geometry": GeometryMetricsEvaluator,
                "text_alignment": TextAlignmentMetricsEvaluator,
            }

            evaluator_class = evaluator_class_map.get(evaluator_type)
            if not evaluator_class:
                logger.warning(f"Unknown evaluator type: {evaluator_type}")
                continue

            supported_metrics = getattr(evaluator_class, "_supported_metrics", [])

            if not set(self.config.metrics) & set(supported_metrics):
                logger.info(
                    f"Skipping {evaluator_type} evaluator (no relevant metrics)"
                )
                continue

            logger.info(f"Initializing and computing {evaluator_type} metrics...")

            try:
                # Create evaluator
                evaluator = self._create_evaluator(evaluator_type)

                # Run evaluation
                if evaluator_type == "image":
                    self.results.update(evaluator.compute(data1, self.config.metrics))
                elif evaluator_type == "distribution":
                    self.results.update(
                        evaluator.compute(data1, data2, self.config.metrics)
                    )
                elif evaluator_type == "video":
                    self.results.update(
                        evaluator.compute(data1, data2, self.config.metrics)
                    )
                elif evaluator_type == "geometry":
                    self.results.update(
                        evaluator.compute(
                            data1, data2[0], data2[1], self.config.metrics
                        )
                    )
                elif evaluator_type == "text_alignment":
                    self.results.update(evaluator.compute(data1, self.config.metrics))

                logger.info(f"{evaluator_type} metrics computation completed")

            except Exception as e:
                logger.error(f"{evaluator_type} evaluator failed: {e}")
                continue
            finally:
                # Clean up evaluator and release GPU memory
                self._cleanup_evaluator(evaluator)
                logger.info(
                    f"{evaluator_type} evaluator cleaned up, GPU memory released"
                )

        # 4. Save results
        self._save_results()

        # 5. Print summary
        self._print_summary()

        return self.results

    def _save_results(self):
        """Save evaluation results"""
        os.makedirs(self.config.output_dir, exist_ok=True)

        # Save detailed results
        if self.config.save_detailed:
            detailed_path = os.path.join(
                self.config.output_dir, "detailed_results.json"
            )
            with open(detailed_path, "w", encoding="utf-8") as f:
                json.dump(self.results, f, indent=2, ensure_ascii=False)
            logger.info(f"Detailed results saved to: {detailed_path}")

        # Save summary results
        summary = self._create_summary()
        summary_path = os.path.join(self.config.output_dir, "summary.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        logger.info(f"Summary results saved to: {summary_path}")

    def _create_summary(self) -> Dict[str, Any]:
        """Create summary results"""
        summary = {
            "config": {
                "gt_root": self.config.gt_root,
                "pred_root": self.config.pred_root,
                "metrics": self.config.metrics,
                "device": self.config.device,
                "image_size": self.config.image_size,
            },
            "results": {},
        }

        for metric, result in self.results.items():
            if isinstance(result, dict) and "mean" in result:
                summary["results"][metric] = {
                    "mean": result["mean"],
                    "std": result.get("std", None),
                    "count": result.get("count", None),
                }
            else:
                summary["results"][metric] = result

        return summary

    def _print_summary(self):
        """Print evaluation summary"""
        logger.info("=" * 50)
        logger.info("Evaluation Results Summary:")
        logger.info("=" * 50)

        for metric, result in self.results.items():
            if isinstance(result, dict) and "mean" in result:
                logger.info(
                    f"{metric.upper()}: {result['mean']:.4f} ± {result.get('std', 0):.4f}"
                )
            else:
                logger.info(f"{metric.upper()}: {result}")

        logger.info("=" * 50)


def main():
    """Main function"""
    parser = argparse.ArgumentParser(description="Edit3D-Bench Evaluation System")

    # Data paths
    parser.add_argument(
        "--gt_root", type=str, required=True, help="Ground truth data root directory"
    )
    parser.add_argument(
        "--pred_root", type=str, required=True, help="Prediction results root directory"
    )

    # Evaluation metrics
    parser.add_argument(
        "--metrics",
        nargs="+",
        choices=["psnr", "ssim", "lpips", "fid", "dino_i", "fvd", "chamfer", "clip_t"],
        default=["psnr", "ssim", "lpips", "fid"],
        help="Evaluation metrics to compute",
    )

    # Configuration parameters
    parser.add_argument("--device", type=str, default="cuda:0", help="Computing device")
    parser.add_argument(
        "--image_size",
        nargs=2,
        type=int,
        default=[512, 512],
        help="Image size (height width)",
    )
    parser.add_argument("--batch_size", type=int, default=32, help="Batch size")
    parser.add_argument(
        "--num_workers", type=int, default=4, help="Number of data loading workers"
    )
    parser.add_argument(
        "--max_images",
        type=int,
        default=None,
        help="Maximum number of images to process",
    )
    parser.add_argument(
        "--max_videos",
        type=int,
        default=None,
        help="Maximum number of videos to process",
    )
    parser.add_argument("--ignore_mask", action="store_true", help="Ignore mask")
    # Output configuration
    parser.add_argument(
        "--output_dir",
        type=str,
        default="evaluation_results",
        help="Results output directory",
    )
    parser.add_argument(
        "--no_detailed", action="store_true", help="Do not save detailed results"
    )

    args = parser.parse_args()

    # Create configuration
    config = EvaluationConfig(
        gt_root=args.gt_root,
        pred_root=args.pred_root,
        metrics=args.metrics,
        device=args.device,
        image_size=tuple(args.image_size),
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        max_images=args.max_images,
        max_videos=args.max_videos,
        output_dir=args.output_dir,
        save_detailed=not args.no_detailed,
        ignore_mask=args.ignore_mask,
    )

    # Run evaluation
    evaluator = EvaluationManager(config)
    results = evaluator.run_evaluation()

    logger.info("Evaluation completed!")


if __name__ == "__main__":
    main()
