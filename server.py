import random
from flask import Flask, request, redirect, url_for, Response, render_template, flash, jsonify
import os
import threading
from werkzeug.utils import secure_filename
import subprocess
import socket
import re, time
import requests
import zipfile
import json
import datetime
import tempfile
import shutil


app = Flask(__name__)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(PROJECT_ROOT, "models")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

#############################################
# Global Server Registry Structure          #
#############################################
# Updated structure for decoupled instances:
# {
#   model_name: {
#     "web_apps": [
#       {
#         "id": str,  # unique instance ID
#         "port": int, 
#         "process": Process, 
#         "status": "running|stopped", 
#         "url": str,
#         "created_at": datetime string,
#         "deploying": Boolean
#       }
#     ],
#     "inference_apps": [
#       {
#         "id": str,  # unique instance ID
#         "port": int, 
#         "process": Process, 
#         "status": "running|stopped", 
#         "url": str,
#         "created_at": datetime string,
#         "deploying": Boolean
#       }
#     ],
#     "model_info": {
#       "descriptor": dict,  # cached descriptor
#       "zip_path": str      # path to zip file
#     }
#   }
# }

app_servers = {}

# Add global deployment lock dictionary
deployment_locks = {}

def log_message(log_file_path, message):
    """
    Logs a message with a timestamp to a specified log file.

    Args:
        log_file_path (str): The file path to the log file.
        message (str): The message to be logged.

    Returns:
        None
    """
    timestamp = datetime.datetime.now().isoformat()
    with open(log_file_path, "a") as lf:
        lf.write(f"[{timestamp}] {message}\n")

def deploy_instance(model_name, zip_path, descriptor, app_type):
    """
    Deploys a single instance of a specific app type (web_app or inference_app).
    Uses absolute paths to ensure consistency across different environments.
    
    Args:
        model_name (str): The name of the model.
        zip_path (str): Path to the deployment ZIP file.
        descriptor (dict): Model descriptor data.
        app_type (str): Type of app to deploy - 'web_app' or 'inference_app'.
        
    Returns:
        dict: Information about the deployed instance.
    """
    import uuid
    import datetime
    
    # Define a unique lock key for this model and app type
    lock_key = f"{model_name}_{app_type}"
    
    # Check if deployment is already in progress
    if lock_key in deployment_locks and deployment_locks[lock_key]:
        raise RuntimeError(f"Deployment of {app_type} for {model_name} is already in progress")
    
    # Set the deployment lock
    deployment_locks[lock_key] = True
    
    try:
        # Generate instance ID
        instance_id = str(uuid.uuid4())
        print(f"Deploying new {app_type} instance {instance_id} for model {model_name}")
        
        # Initialize model in registry if not exists
        if model_name not in app_servers:
            app_servers[model_name] = {
                "web_apps": [],
                "inference_apps": [],
                "model_info": {
                    "descriptor": descriptor,
                    "zip_path": zip_path
                }
            }
        
        # Create instance directory using absolute paths
        deployed_dir = os.path.join(PROJECT_ROOT, "deployed_models", model_name)
        os.makedirs(deployed_dir, exist_ok=True)
        
        # Allocate port
        port = 0
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            port = s.getsockname()[1]
        
        # Create instance record
        instance = {
            "id": instance_id,
            "port": port,
            "process": None,
            "status": "initializing",
            "url": f"http://localhost:{port}",
            "created_at": datetime.datetime.now().isoformat(),
            "deploying": True
        }
        
        # Add to registry based on app type
        if app_type == "web_app":
            app_servers[model_name]["web_apps"].append(instance)
        elif app_type == "inference_app":
            app_servers[model_name]["inference_apps"].append(instance)
        else:
            raise ValueError(f"Invalid app_type: {app_type}")
        
        # Create app directory with absolute path
        app_dir = os.path.join(deployed_dir, f"{app_type}_{instance_id}")
        os.makedirs(app_dir, exist_ok=True)
        
        # Extract app files from zip
        with tempfile.TemporaryDirectory() as temp_dir:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(temp_dir)
            
            # Copy app-specific files
            app_src = os.path.join(temp_dir, app_type)
            if os.path.exists(app_src):
                for item in os.listdir(app_src):
                    s = os.path.join(app_src, item)
                    d = os.path.join(app_dir, item)
                    if os.path.isdir(s):
                        shutil.copytree(s, d, dirs_exist_ok=True)
                    else:
                        shutil.copy2(s, d)
        
        # Create app-specific descriptor
        app_descriptor = descriptor.copy()
        app_descriptor["deployed_at"] = datetime.datetime.now().isoformat()
        app_descriptor["instance_id"] = instance_id
        app_descriptor["app_type"] = app_type
        app_descriptor["port"] = port
        app_descriptor["app_dir"] = app_dir  # Store absolute directory path
        
        # For web app, find an available inference API
        available_inference_api = None
        if app_type == "web_app" and model_name in app_servers:
            for inf_app in app_servers[model_name]["inference_apps"]:
                if inf_app["status"] == "running":
                    available_inference_api = inf_app["url"]
                    app_descriptor["inference_api_url"] = available_inference_api
                    break
        
        # Write descriptor file
        descriptor_path = os.path.join(app_dir, "descriptor.json")
        with open(descriptor_path, "w") as f:
            json.dump(app_descriptor, f, indent=4)
        
        # Setup logging with absolute path
        log_file = os.path.join(app_dir, "app.log")
        log_message(log_file, f"Setting up {app_type} for {model_name} on port {port}")
        log_message(log_file, f"Application directory: {app_dir}")
        
        # Create and setup virtual environment with absolute path
        venv_dir = os.path.join(app_dir, "venv")
        subprocess.run(["python", "-m", "venv", venv_dir], check=True)
        
        # Get platform-specific pip path
        pip = os.path.join(venv_dir, "bin", "pip") if os.name != "nt" else os.path.join(venv_dir, "Scripts", "pip")
        subprocess.run([pip, "install", "--upgrade", "pip"], check=True)
        
        # Install dependencies one by one
        log_message(log_file, f"Installing dependencies for {app_type}")
        
        # Try to install from descriptor
        requirements_installed = False
        req_key = app_type if app_type in descriptor.get("requirements", {}) else None
        
        if req_key and descriptor["requirements"].get(req_key):
            log_message(log_file, f"Installing dependencies from descriptor ({len(descriptor['requirements'][req_key])} packages)")
            for req in descriptor["requirements"][req_key]:
                try:
                    log_message(log_file, f"Installing: {req}")
                    subprocess.run([pip, "install", req], check=True)
                    log_message(log_file, f"Successfully installed: {req}")
                except Exception as e:
                    log_message(log_file, f"Error installing {req}: {e}")
        
        # Try requirements.txt if available
        req_file = os.path.join(app_dir, "requirements.txt")
        if os.path.exists(req_file):
            log_message(log_file, f"Installing dependencies from requirements.txt: {req_file}")
            try:
                # Read requirements.txt file and install packages one by one
                with open(req_file, "r") as f:
                    requirements = [line.strip() for line in f if line.strip() and not line.startswith("#")]
                
                log_message(log_file, f"Found {len(requirements)} packages in requirements.txt")
                for req in requirements:
                    try:
                        log_message(log_file, f"Installing: {req}")
                        subprocess.run([pip, "install", req], check=True)
                        log_message(log_file, f"Successfully installed: {req}")
                    except Exception as e:
                        log_message(log_file, f"Error installing {req}: {e}")
                
                requirements_installed = True
                log_message(log_file, "Completed installing dependencies from requirements.txt")
            except Exception as e:
                log_message(log_file, f"Error reading requirements.txt: {e}")
        
        # Launch the app using absolute paths
        python_path = os.path.join(venv_dir, "bin", "python") if os.name != "nt" else os.path.join(venv_dir, "Scripts", "python")
        env_vars = os.environ.copy()
        env_vars["PORT"] = str(port)
        env_vars["FLASK_RUN_PORT"] = str(port)
        env_vars["MODEL_NAME"] = model_name
        env_vars["INSTANCE_ID"] = instance_id
        env_vars["APP_DIR"] = app_dir  # Pass the app directory as an environment variable
        
        # Set inference API URL for web app
        if app_type == "web_app" and available_inference_api:
            env_vars["INFERENCE_API_URL"] = available_inference_api
        
        # Determine app file with absolute path
        app_file = "app.py"
        if os.path.exists(os.path.join(app_dir, "app.py")):
            app_file = "app.py"
        elif os.path.exists(os.path.join(app_dir, f"{app_type.split('_')[0]}.py")):
            app_file = f"{app_type.split('_')[0]}.py"
        
        app_file_path = os.path.join(app_dir, app_file)
        env_vars["FLASK_APP"] = app_file_path  # Use absolute path for Flask app
        
        # Log the command that will be executed
        log_message(log_file, f"Running command: {python_path} -m flask run --host=0.0.0.0 --port {port}")
        log_message(log_file, f"Working directory: {app_dir}")
        log_message(log_file, f"App file: {app_file_path}")
        
        # Launch process with absolute paths
        proc = subprocess.Popen(
            [python_path, "-m", "flask", "run", "--host=0.0.0.0", "--port", str(port)],
            env=env_vars,
            cwd=app_dir,  # Use absolute path for working directory
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True
        )
        
        log_message(log_file, f"{app_type} process started with PID {proc.pid}")
        
        # Update instance record
        instance["process"] = proc
        instance["status"] = "running"
        instance["deploying"] = False
        instance["app_dir"] = app_dir  # Store absolute path in instance record
        
        return instance
    except Exception as e:
        print(f"Error deploying {app_type} instance for {model_name}: {str(e)}")
        log_file = os.path.join(deployed_dir, f"{app_type}_{instance_id}", "app.log")
        if os.path.exists(os.path.dirname(log_file)):
            log_message(log_file, f"Deployment error: {str(e)}")
        raise
    finally:
        # Release the lock regardless of success or failure
        deployment_locks[lock_key] = False

# Add a background deployment function
def deploy_in_background(model_name, zip_path, descriptor, app_type=None):
    """
    Deploy model components in background thread.
    When app_type is None, deploys both web and inference apps simultaneously.
    
    Args:
        model_name (str): Model name
        zip_path (str): Path to deployment package
        descriptor (dict): Model descriptor
        app_type (str, optional): Type of app to deploy ('web_app' or 'inference_app'). 
                                 If None, deploys both components simultaneously.
    """
    try:
        if app_type:
            # Deploy single component
            instance = deploy_instance(model_name, zip_path, descriptor, app_type)
            
            # Update descriptor with new instance info
            release_folder = os.path.join(UPLOAD_FOLDER, secure_filename(model_name), "release")
            descriptor_path = os.path.join(release_folder, "descriptor.json")
            
            with open(descriptor_path, 'r') as f:
                descriptor_data = json.load(f)
            
            if "instances" not in descriptor_data:
                descriptor_data["instances"] = []
                
            descriptor_data["instances"].append({
                "id": instance["id"],
                "type": app_type,
                "port": instance["port"],
                "created_at": instance["created_at"]
            })
            
            with open(descriptor_path, 'w') as f:
                json.dump(descriptor_data, f, indent=4)
                
            print(f"Successfully deployed {app_type} for {model_name}")
        else:
            # Deploy both components simultaneously using threads
            results = {"web_app": None, "inference_app": None}
            deployment_errors = []
            
            def deploy_component(component_type):
                try:
                    results[component_type] = deploy_instance(model_name, zip_path, descriptor, component_type)
                    print(f"Successfully deployed {component_type} for {model_name}")
                except Exception as e:
                    error_msg = f"Error deploying {component_type} for {model_name}: {str(e)}"
                    print(error_msg)
                    deployment_errors.append(error_msg)
            
            # Create threads for simultaneous deployment
            web_thread = threading.Thread(target=deploy_component, args=("web_app",))
            inf_thread = threading.Thread(target=deploy_component, args=("inference_app",))
            
            # Start both deployments
            web_thread.start()
            inf_thread.start()
            
            # Wait for both to complete
            web_thread.join()
            inf_thread.join()
            
            # Check for errors
            if deployment_errors:
                print(f"Deployment completed with errors: {deployment_errors}")
            
            # Update descriptor if both deployments were successful
            if results["web_app"] and results["inference_app"]:
                release_folder = os.path.join(UPLOAD_FOLDER, secure_filename(model_name), "release")
                descriptor_path = os.path.join(release_folder, "descriptor.json")
                
                with open(descriptor_path, 'r') as f:
                    descriptor_data = json.load(f)
                
                if "instances" not in descriptor_data:
                    descriptor_data["instances"] = []
                    
                descriptor_data["instances"].append({
                    "web_app": {
                        "id": results["web_app"]["id"],
                        "port": results["web_app"]["port"]
                    },
                    "inference_app": {
                        "id": results["inference_app"]["id"],
                        "port": results["inference_app"]["port"]
                    },
                    "created_at": datetime.datetime.now().isoformat()
                })
                
                with open(descriptor_path, 'w') as f:
                    json.dump(descriptor_data, f, indent=4)
                    
                # Since inference_app might have been deployed first,
                # ensure the web_app is connected to it
                if model_name in app_servers:
                    for web_app in app_servers[model_name]["web_apps"]:
                        if web_app["id"] == results["web_app"]["id"]:
                            for inf_app in app_servers[model_name]["inference_apps"]:
                                if inf_app["id"] == results["inference_app"]["id"]:
                                    # Add or update the connection to the inference API
                                    inf_url = inf_app["url"]
                                    web_dir = web_app.get("app_dir")
                                    if web_dir:
                                        # Update the descriptor.json in the web app directory
                                        web_desc_path = os.path.join(web_dir, "descriptor.json")
                                        if os.path.exists(web_desc_path):
                                            with open(web_desc_path, 'r') as f:
                                                web_desc = json.load(f)
                                            web_desc["inference_api_url"] = inf_url
                                            with open(web_desc_path, 'w') as f:
                                                json.dump(web_desc, f, indent=4)
                                    break
                            break
                
                print(f"Successfully deployed model {model_name} with both components")
            else:
                print(f"Deployment of {model_name} completed but one or more components failed")
    except Exception as e:
        print(f"Error in background deployment: {str(e)}")

def deploy_model(model_name, zip_path, descriptor):
    """
    Deploy both web app and inference app as separate instances simultaneously.
    """
    results = {"web_app": None, "inference_app": None}
    errors = []
    
    def deploy_component(component_type):
        try:
            results[component_type] = deploy_instance(model_name, zip_path, descriptor, component_type)
        except Exception as e:
            error_msg = f"Error deploying {component_type}: {str(e)}"
            errors.append(error_msg)
    
    # Create threads for parallel deployment
    web_thread = threading.Thread(target=deploy_component, args=("web_app",))
    inf_thread = threading.Thread(target=deploy_component, args=("inference_app",))
    
    # Start threads
    inf_thread.start()
    web_thread.start()
    
    # Wait for completion
    inf_thread.join()
    web_thread.join()
    
    # Check for errors
    if errors:
        raise Exception("; ".join(errors))
    
    # Return results
    return {
        "web_app": {
            "instance_id": results["web_app"]["id"],
            "port": results["web_app"]["port"]
        },
        "inference_app": {
            "instance_id": results["inference_app"]["id"],
            "port": results["inference_app"]["port"]
        }
    }

#############################################
# Packaging Function                       #
#############################################

def package_model(model_name, web_app_zip, inference_app_zip):
    """
    Packages web app (frontend) and inference app (API backend) into a model release.
    Also creates a descriptor.json file containing model metadata.

    Args:
        model_name (str): The name of the model to be packaged.
        web_app_zip (FileStorage): The uploaded zip file for the web application.
        inference_app_zip (FileStorage): The uploaded zip file for the inference application.

    Returns:
        tuple: A tuple containing:
            - descriptor (dict): The descriptor metadata for the model.
            - zip_path (str): The file path to the created ZIP package.
    """
    from datetime import datetime
    import json
    model_folder = os.path.join(UPLOAD_FOLDER, secure_filename(model_name))
    release_folder = os.path.join(model_folder, "release")
    src_folder = os.path.join(model_folder, "src")
    web_app_folder = os.path.join(src_folder, "web_app")
    inference_app_folder = os.path.join(src_folder, "inference_app")
    
    # Create necessary directories with absolute paths
    os.makedirs(release_folder, exist_ok=True)
    os.makedirs(src_folder, exist_ok=True)
    os.makedirs(web_app_folder, exist_ok=True)
    os.makedirs(inference_app_folder, exist_ok=True)
    
    # Log the absolute paths being used
    print(f"Model folder: {model_folder}")
    print(f"Release folder: {release_folder}")
    print(f"Web app folder: {web_app_folder}")
    print(f"Inference app folder: {inference_app_folder}")

    # Extract web app zip
    web_app_path = os.path.join(web_app_folder, "web_app.zip")
    web_app_zip.save(web_app_path)
    with zipfile.ZipFile(web_app_path, "r") as zip_ref:
        zip_ref.extractall(web_app_folder)
    
    # Extract inference app zip
    inference_app_path = os.path.join(inference_app_folder, "inference_app.zip")
    inference_app_zip.save(inference_app_path)
    with zipfile.ZipFile(inference_app_path, "r") as zip_ref:
        zip_ref.extractall(inference_app_folder)
    
    # Read requirements from both apps - FIX: Improved requirement file reading
    web_app_requirements = []
    inference_app_requirements = []
    
    # Check for requirements.txt in web_app folder
    web_app_req_path = os.path.join(web_app_folder, "requirements.txt")
    if os.path.exists(web_app_req_path):
        with open(web_app_req_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    # Strip version specifiers to get base package name
                    package = re.split(r'[=<>]', line)[0].strip()
                    if package:
                        web_app_requirements.append(package)
        print(f"Web app requirements: {web_app_requirements}")
    else:
        print(f"Warning: requirements.txt not found in {web_app_folder}")
    
    # Check for requirements.txt in inference_app folder
    inference_app_req_path = os.path.join(inference_app_folder, "requirements.txt")
    if os.path.exists(inference_app_req_path):
        with open(inference_app_req_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    # Strip version specifiers to get base package name
                    package = re.split(r'[=<>]', line)[0].strip()
                    if package:
                        inference_app_requirements.append(package)
        print(f"Inference app requirements: {inference_app_requirements}")
    else:
        print(f"Warning: requirements.txt not found in {inference_app_folder}")
    
    # Combine unique requirements from both apps
    all_requirements = list(set(web_app_requirements + inference_app_requirements))
    
    # Find model weights files
    model_weights = []
    for root, _, files in os.walk(inference_app_folder):
        for file in files:
            if file.endswith((".pt", ".pth", ".onnx", ".h5")):
                rel_path = os.path.relpath(os.path.join(root, file), inference_app_folder)
                model_weights.append(rel_path)

    # Create descriptor with comprehensive metadata including absolute paths
    descriptor = {
        "model_name": model_name,
        "version": request.form.get("version", "1.0"),
        "created_at": datetime.now().isoformat(),
        "author": request.form.get("author", "Unknown"),
        "description": request.form.get("description", f"Model {model_name}"),
        "project_root": PROJECT_ROOT,
        "paths": {
            "model_folder": model_folder,
            "release_folder": release_folder,
            "web_app_folder": web_app_folder,
            "inference_app_folder": inference_app_folder
        },
        "files": {
            "web_app_folder": "web_app",
            "inference_app_folder": "inference_app",
            "model_weights": model_weights
        },
        "requirements": {
            "combined": all_requirements,
            "web_app": web_app_requirements,
            "inference_app": inference_app_requirements
        },
        "interface_type": "dual",  # Indicates both web and inference apps
        "app_relationship": {
            "web_app": "frontend",
            "inference_app": "api_backend"
        },
        "instances": [],  # Will store info about instances, not just ports
        "api_endpoints": {
            "predict": {
                "method": "POST",
                "description": "Make predictions using the model",
                "parameters": "Depends on the specific model implementation"
            },
            "health": {
                "method": "GET",
                "description": "Check if the API is running properly"
            }
        }
    }

    # Save descriptor.json in each app folder and release folder
    descriptor_path = os.path.join(release_folder, "descriptor.json")
    with open(descriptor_path, 'w') as f:
        json.dump(descriptor, f, indent=4)
        
    # Also save copies in web_app and inference_app folders for reference
    with open(os.path.join(web_app_folder, "descriptor.json"), 'w') as f:
        json.dump(descriptor, f, indent=4)
        
    with open(os.path.join(inference_app_folder, "descriptor.json"), 'w') as f:
        json.dump(descriptor, f, indent=4)

    # Create final zip package for the whole model
    zip_filename = f"{secure_filename(model_name)}.zip"
    zip_path = os.path.join(release_folder, zip_filename)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        # Add descriptor.json at the root level of the ZIP
        zipf.write(descriptor_path, arcname="descriptor.json")
        
        # Add web app files, including requirements.txt
        for root, _, files in os.walk(web_app_folder):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.join("web_app", os.path.relpath(file_path, web_app_folder))
                zipf.write(file_path, arcname=arcname)
                
        # Add inference app files, including requirements.txt
        for root, _, files in os.walk(inference_app_folder):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.join("inference_app", os.path.relpath(file_path, inference_app_folder))
                zipf.write(file_path, arcname=arcname)
                
    return descriptor, zip_path

#############################################
# Flask Routes                              #
#############################################

@app.route("/")
def index():
    """
    Renders the home page of the application.

    Returns:
        Response: The rendered HTML template for the home page.
    """
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload_model():
    """
    Handles uploading both web and inference app packages for a model.
    Starts deployment in background and redirects to appropriate page.
    """
    required_files = ["web_app", "inference_app"]
    for file_key in required_files:
        if file_key not in request.files:
            return f"No file provided for {file_key}", 400

    model_name = request.form.get("model_name", "").strip()
    if not model_name:
        return "Model name is required", 400

    web_app_file = request.files["web_app"]
    inference_app_file = request.files["inference_app"]
    
    if web_app_file.filename == "" or inference_app_file.filename == "":
        return "One or more files were not selected", 400

    descriptor, zip_path = package_model(model_name, web_app_file, inference_app_file)
    
    # Start deployment in background thread
    threading.Thread(
        target=deploy_in_background, 
        args=(model_name, zip_path, descriptor)
    ).start()
    
    # Redirect to the deployment status page
    return render_template("deployment_status.html", 
                          model_name=model_name,
                          descriptor=descriptor,
                          message="Model is being deployed. Please wait...",
                          redirect_url=url_for('model_specific', model_name=model_name),
                          redirect_seconds=5)

@app.route("/models", methods=["GET"])
def list_models():
    """
    Lists all available models in the system.

    Args:
        None

    Returns:
        Response: The rendered HTML template displaying the list of models.
    """
    models = os.listdir(UPLOAD_FOLDER)
    return render_template("models.html", models=models)


@app.route("/model/<model_name>", methods=["GET"])
def model_specific(model_name):
    """
    Displays the interface for a specific model, showing the web app frontend.
    If model is deploying or not running, shows appropriate status.
    """
    if model_name not in os.listdir(UPLOAD_FOLDER):
        return "Model not found", 404

    descriptor_path = os.path.join(UPLOAD_FOLDER, model_name, "release/descriptor.json")
    if not os.path.exists(descriptor_path):
        return f"Descriptor file for model {model_name} not found", 404
    
    with open(descriptor_path, 'r') as f:
        descriptor = json.load(f)

    # Check for deployment locks first, but only redirect to deployment status
    # if we don't have any running instances already
    lock_key_web = f"{model_name}_web_app"
    lock_key_inf = f"{model_name}_inference_app"
    
    # Check if model has instances in app_servers
    has_running_web_app = False
    if model_name in app_servers:
        # Check for running web app instance
        for instance in app_servers[model_name]["web_apps"]:
            if not instance["deploying"] and instance["status"] == "running":
                has_running_web_app = True
                break
    
    # Only show deployment status if no running web app instances and deployment in progress
    if not has_running_web_app and (
        (lock_key_web in deployment_locks and deployment_locks[lock_key_web]) or
        (lock_key_inf in deployment_locks and deployment_locks[lock_key_inf])
    ):
        return render_template("deployment_status.html", 
                          model_name=model_name,
                          descriptor=descriptor,
                          message="Model deployment is in progress. Please wait...",
                          redirect_url=url_for('model_specific', model_name=model_name),
                          redirect_seconds=5)
    
    # Check if model has instances in app_servers
    if model_name in app_servers:
        # Find a running web app instance
        web_candidates = [
            instance for instance in app_servers[model_name]["web_apps"]
            if not instance["deploying"] and instance["status"] == "running"
        ]
        web_instance = random.choice(web_candidates) if web_candidates else None

        inf_candidates = [
            instance for instance in app_servers[model_name]["inference_apps"]
            if not instance["deploying"] and instance["status"] == "running"
        ]
        inf_instance = random.choice(inf_candidates) if inf_candidates else None
        
        # If we have a web app, show it
        if web_instance:
            return render_template("model_interface.html", 
                                  model_name=model_name,
                                  descriptor=descriptor,
                                  is_dual_app=True,
                                  instance_id=web_instance["id"],
                                  web_app_port=web_instance["port"], 
                                  web_app_url=web_instance["url"],
                                  inference_app_url=inf_instance["url"] if inf_instance else None,
                                  inference_app_port=inf_instance["port"] if inf_instance else None)
        
        # Check if anything is being deployed
        deploying_web = any(i["deploying"] for i in app_servers[model_name]["web_apps"])
        if deploying_web:
            return render_template("model_interface.html", 
                              model_name=model_name,
                              descriptor=descriptor,
                              is_deploying=True,
                              deployment_message="Web application is currently being deployed. Please wait...")
    
    # Check for lock file
    # ...existing lock file code...
    
    # Deploy both components if nothing is running
    try:
        # Deploy new instances
        import glob
        release_folder = os.path.join(UPLOAD_FOLDER, model_name, "release")
        zip_files = glob.glob(os.path.join(release_folder, "*.zip"))
        
        if not zip_files:
            return "No deployment package found", 404
            
        zip_path = zip_files[0]
        
        # Start deployment in background
        threading.Thread(
            target=deploy_in_background,
            args=(model_name, zip_path, descriptor)
        ).start()
        
        # Show deployment status page
        return render_template("deployment_status.html", 
                          model_name=model_name,
                          descriptor=descriptor,
                          message="Starting model deployment. This may take a few minutes...",
                          redirect_url=url_for('model_specific', model_name=model_name),
                          redirect_seconds=5)
    except Exception as e:
        return f"Error starting deployment: {str(e)}", 500

@app.route("/model/<model_name>/api_doc")
def api_doc_model(model_name):
    """
    Displays the API documentation for a specific model, including dynamically fetched details from running inference instances.

    Args:
        model_name (str): The name of the model for which API documentation is to be displayed.

    Returns:
        Response: The rendered HTML template for the API documentation.
    """
    import json

    # Verify that the model exists
    if model_name not in os.listdir(UPLOAD_FOLDER):
        return "Model not found", 404

    # Determine the path to the descriptor file.
    descriptor_path = os.path.join(UPLOAD_FOLDER, model_name, "release", "descriptor.json")
    if not os.path.exists(descriptor_path):
        return f"Descriptor file for model {model_name} not found", 404

    with open(descriptor_path, "r") as f:
        descriptor = json.load(f)

    # Build API endpoints information dynamically.
    api_endpoints = {
        "Model API": url_for("proxy_model_api", model_name=model_name, subpath="", _external=True),
        "API Doc": url_for("api_doc_model", model_name=model_name, _external=True),
        "Instances": url_for("instances_model", model_name=model_name, _external=True)
    }
    num_endpoints = descriptor.get("num_api_endpoints", len(api_endpoints))
    code_snippets = descriptor.get("code_snippets", None)

    # Retrieve detailed API documentation from one of the running inference app instances.
    detailed_api_docs = None
    if model_name in app_servers:
        for instance in app_servers[model_name].get("inference_apps", []):
            if not instance["deploying"] and instance["status"] == "running":
                port = instance.get("port")
                try:
                    # Attempt to fetch API definition from the inference instance.
                    response = requests.get(f"http://127.0.0.1:{port}/gradio_api/info", timeout=5)
                    response.raise_for_status()
                    detailed_api_docs = response.json()
                except Exception as e:
                    detailed_api_docs = f"Error fetching API definition from instance at port {port}: {e}"
                break

    if not detailed_api_docs:
        detailed_api_docs = "No running inference instances available for API definition."

    return render_template(
        "api_doc.html",
        model_name=model_name,
        descriptor=descriptor,
        api_endpoints=api_endpoints,
        num_endpoints=num_endpoints,
        code_snippets=code_snippets,
        detailed_api_docs=detailed_api_docs
    )


@app.route("/model/<model_name>/instances")
def instances_model(model_name):
    """
    Displays all instances of a specific model, with details and status information.
    Uses absolute paths for log files and other operations.
    """
    # Get model descriptor
    descriptor_path = os.path.join(UPLOAD_FOLDER, model_name, "release/descriptor.json")
    descriptor = None
    if os.path.exists(descriptor_path):
        with open(descriptor_path, "r") as f:
            descriptor = json.load(f)
    
    instances = []
    
    # Get all instances from app_servers
    if model_name in app_servers:
        # Get web app instances
        for instance in app_servers[model_name]["web_apps"]:
            instance_id = instance["id"]
            # Use absolute path for app directory
            app_dir = instance.get("app_dir") or os.path.join(PROJECT_ROOT, "deployed_models", model_name, f"web_app_{instance_id}")
            log_file = os.path.join(app_dir, "app.log")
            logs = ""
            
            if os.path.exists(log_file):
                with open(log_file, "r") as f:
                    logs = f.read()
            
            instances.append({
                "instance_id": instance_id,
                "type": "Web App (Frontend)",
                "port": instance["port"],
                "url": instance["url"],
                "status": instance["status"],
                "deployed_at": instance["created_at"],
                "logs": logs,
                "app_dir": app_dir  # Include absolute path in instance data
            })
        
        # Get inference app instances
        for instance in app_servers[model_name]["inference_apps"]:
            instance_id = instance["id"]
            # Use absolute path for app directory
            app_dir = instance.get("app_dir") or os.path.join(PROJECT_ROOT, "deployed_models", model_name, f"inference_app_{instance_id}")
            log_file = os.path.join(app_dir, "app.log")
            logs = ""
            
            if os.path.exists(log_file):
                with open(log_file, "r") as f:
                    logs = f.read()
            
            instances.append({
                "instance_id": instance_id,
                "type": "Inference API (Backend)",
                "port": instance["port"],
                "url": instance["url"],
                "status": instance["status"],
                "deployed_at": instance["created_at"],
                "logs": logs,
            })

    return render_template("instances.html", 
                        model_name=model_name, 
                        instances=instances, 
                        descriptor=descriptor,
                        is_dual_app=True)

@app.route("/model/<model_name>/create_instance", methods=["POST"])
def create_model_instance(model_name):
    """
    Creates a new instance of a model component (web app or inference app).
    Prevents multiple simultaneous deployments using lock.
    """
    app_type = request.form.get("app_type", "web_app")
    if app_type not in ["web_app", "inference_app"]:
        return "Invalid app type. Must be 'web_app' or 'inference_app'.", 400
    
    if model_name not in os.listdir(UPLOAD_FOLDER):
        return "Model not found", 404
        
    descriptor_path = os.path.join(UPLOAD_FOLDER, model_name, "release/descriptor.json")
    if not os.path.exists(descriptor_path):
        return f"Descriptor file for model {model_name} not found", 404
        
    with open(descriptor_path, 'r') as f:
        descriptor = json.load(f)
    
    # Check if deployment is already in progress
    lock_key = f"{model_name}_{app_type}"
    if lock_key in deployment_locks and deployment_locks[lock_key]:
        return render_template("deployment_status.html", 
                          model_name=model_name,
                          descriptor=descriptor,
                          message=f"{app_type} deployment is already in progress. Please wait...",
                          redirect_url=url_for('instances_model', model_name=model_name),
                          redirect_seconds=5)
    
    # Start deployment in background
    zip_path = os.path.join(UPLOAD_FOLDER, model_name, "release", f"{model_name}.zip")
    threading.Thread(
        target=deploy_in_background,
        args=(model_name, zip_path, descriptor, app_type)
    ).start()
    
    # Show deployment status page
    return render_template("deployment_status.html", 
                      model_name=model_name,
                      descriptor=descriptor,
                      message=f"Starting {app_type} deployment. This may take a few minutes...",
                      redirect_url=url_for('instances_model', model_name=model_name),
                      redirect_seconds=5)

@app.route("/model/<model_name>/stop_instance", methods=["POST"])
def stop_instance(model_name):
    """
    Stops a specific instance of a model component.
    """
    instance_id = request.form.get("instance_id")
    instance_type = request.form.get("instance_type", "")
    
    if not instance_id:
        return "Instance ID is required", 400
    
    if model_name not in app_servers:
        return "Model not found in registry", 404
    
    # Determine correct instance type
    app_list_key = "web_apps" if "Web App" in instance_type else "inference_apps"
    
    # Find and stop the instance
    found = False
    for instance in app_servers[model_name][app_list_key]:
        if instance["id"] == instance_id:
            found = True
            if instance["process"]:
                try:
                    instance["process"].terminate()
                    instance["status"] = "stopped"
                except:
                    pass
            break
    
    if not found:
        return f"Instance {instance_id} not found", 404
    
    return redirect(url_for("instances_model", model_name=model_name))

@app.route("/model/<model_name>/status", methods=["GET"])
def model_status(model_name):
    """
    Returns the current deployment status for a model.
    Used by the frontend to check if a model is being deployed.
    Shows deploying status only when no running instances are available.
    """
    status = {
        "model_name": model_name,
        "deploying": False,
        "instances": []
    }
    
    # Track if we have at least one running instance of each type
    has_running_web_app = False
    has_running_inference_app = False
    
    # Check if model exists in registry
    if model_name in app_servers:
        # Get web app instances status
        web_apps = app_servers[model_name].get("web_apps", [])
        for app in web_apps:
            if app["status"] == "running":
                has_running_web_app = True
                status["instances"].append({
                    "type": "web_app",
                    "id": app["id"],
                    "port": app["port"],
                    "url": app["url"]
                })
                
        # Get inference app instances status
        inf_apps = app_servers[model_name].get("inference_apps", [])
        for app in inf_apps:
            if app["status"] == "running":
                has_running_inference_app = True
                status["instances"].append({
                    "type": "inference_app",
                    "id": app["id"],
                    "port": app["port"],
                    "url": app["url"]
                })
        
        # Check if any apps are deploying and if we're missing a running instance
        is_deploying = (
            (any(app["deploying"] for app in web_apps) and not has_running_web_app) or
            (any(app["deploying"] for app in inf_apps) and not has_running_inference_app)
        )
        status["deploying"] = is_deploying
    
    # Check deployment locks, but only set deploying=True if we don't have running instances
    lock_key_web = f"{model_name}_web_app"
    lock_key_inf = f"{model_name}_inference_app"
    
    if not (has_running_web_app and has_running_inference_app):
        if (lock_key_web in deployment_locks and deployment_locks[lock_key_web]) or \
           (lock_key_inf in deployment_locks and deployment_locks[lock_key_inf]):
            status["deploying"] = True
    
    return jsonify(status)

@app.route("/model/<model_name>/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def proxy_model_api(model_name, subpath):
    """
    Proxies API requests to an available inference API backend instance.
    """
    from flask import jsonify
    import random

    # Check if any inference APIs are available
    available_instance = None

    if model_name in app_servers:
        available_instances = [
            instance
            for instance in app_servers[model_name]["inference_apps"]
            if not instance["deploying"] and instance["status"] == "running"
        ]
        if available_instances:
            available_instance = random.choice(available_instances)

    # If no instance available, try to deploy one
    if not available_instance:
        descriptor_path = os.path.join(UPLOAD_FOLDER, model_name, "release/descriptor.json")
        zip_path = os.path.join(UPLOAD_FOLDER, model_name, "release", f"{model_name}.zip")

        if not os.path.exists(descriptor_path) or not os.path.exists(zip_path):
            return jsonify({"error": "Model not found or not properly packaged"}), 404

        try:
            with open(descriptor_path, 'r') as f:
                descriptor = json.load(f)
            available_instance = deploy_instance(model_name, zip_path, descriptor, "inference_app")
        except Exception as e:
            return jsonify({"error": f"{str(e)}"}), 500

    # Make the request to the inference API
    port = available_instance["port"]
    target_url = f"http://localhost:{port}/{subpath}"

    resp = requests.request(
        method=request.method,
        url=target_url,
        params=dict(request.args),
        headers={key: value for key, value in request.headers if key != "Host"},
        data=request.get_data(),
        cookies=request.cookies,
        allow_redirects=False
    )

    excluded_headers = ["content-encoding", "content-length", "transfer-encoding", "connection"]
    headers = [(name, value) for name, value in resp.raw.headers.items() if name.lower() not in excluded_headers]

    return Response(resp.content, resp.status_code, headers)

if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False)
