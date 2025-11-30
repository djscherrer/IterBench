import logging
import pathlib
import shutil
import subprocess
from typing import Literal

from env.base import Env
import logging

_TEMPLATES_DIR = pathlib.Path(__file__).parent.parent.parent / ".env_templates"


def get_template_dir(env: Env) -> pathlib.Path:
    return _TEMPLATES_DIR / env.id.replace("/", "-")


def prepare_template(env: Env, logger: logging.Logger) -> pathlib.Path:
    template_dir = get_template_dir(env)
    
    if template_dir.exists():
        logger.info(f"Template already exists: {template_dir}")
        return template_dir
    
    logger.info(f"Creating template for {env.id} at {template_dir}")
    template_dir.mkdir(parents=True, exist_ok=True)
    
    # Write manifest files
    for filename, content in env.manifest_files.items():
        (template_dir / filename).write_text(content)
        logger.info(f"  Created {filename}")
    
    # Install dependencies based on language
    if env.language == "Python":
        _prepare_python_template(template_dir, logger)
    elif env.language == "JavaScript":
        _prepare_javascript_template(template_dir, logger)
    else:
        logger.warning(f"Unknown language: {env.language}, skipping dependency installation")
    
    logger.info(f"✓ Template ready: {template_dir}")
    return template_dir


def copy_template_to_workspace(env: Env, workspace_dir: pathlib.Path, logger: logging.Logger) -> None:
    template_dir = get_template_dir(env)
    
    if not template_dir.exists():
        raise FileNotFoundError(f"Template not found: {template_dir}.")
    
    logger.info(f"Copying template from {template_dir} to {workspace_dir}")
    
    for filename in env.manifest_files.keys():
        src = template_dir / filename
        if src.exists():
            shutil.copy2(src, workspace_dir / filename)
            logger.info(f"  Copied {filename}")
        else:
            logger.warning(f"File not found: {src}.")
    
    if env.language == "Python":
        _copy_python_deps(template_dir, workspace_dir, logger)
    elif env.language == "JavaScript":
        _copy_javascript_deps(template_dir, workspace_dir, logger)
    
    logger.info(f"Workspace ready: {workspace_dir}")


def _prepare_python_template(template_dir: pathlib.Path, logger: logging.Logger) -> None:
    logger.info("  Installing Python dependencies...")
    venv_dir = template_dir / "venv"
    
    subprocess.run(["python3", "-m", "venv", str(venv_dir)], check=True)
    
    pip = venv_dir / "bin" / "pip"
    requirements = template_dir / "requirements.txt"
    subprocess.run([str(pip), "install", "-r", str(requirements)], 
                   check=True, capture_output=True)
    
    logger.info("  Python venv created")


def _copy_python_deps(template_dir: pathlib.Path, workspace_dir: pathlib.Path, logger: logging.Logger) -> None:
    src_venv = template_dir / "venv"
    dst_venv = workspace_dir / "venv"
    
    if src_venv.exists():
        shutil.copytree(src_venv, dst_venv, dirs_exist_ok=True)
        logger.info("  Copied Python venv")


def _prepare_javascript_template(template_dir: pathlib.Path, logger: logging.Logger) -> None:
    logger.info("  Installing Node.js dependencies...")
    subprocess.run(["npm", "install", "--silent"], 
                   cwd=template_dir, check=True, capture_output=True)
    logger.info("  node_modules created")


def _copy_javascript_deps(template_dir: pathlib.Path, workspace_dir: pathlib.Path, logger: logging.Logger) -> None:
    src_modules = template_dir / "node_modules"
    dst_modules = workspace_dir / "node_modules"
    
    if src_modules.exists():
        shutil.copytree(src_modules, dst_modules, dirs_exist_ok=True)
        logger.info("  Copied node_modules")


def prepare_all_templates(envs: list[Env]) -> None:
    logger = logging.getLogger("template_prep")
    logger.info(f"Preparing templates...")
    for env in envs:
        try:
            prepare_template(env, logger)
        except Exception as e:
            logger.error(f"Failed to prepare template for {env.id}: {e}")
    logger.info("  All templates prepared")
