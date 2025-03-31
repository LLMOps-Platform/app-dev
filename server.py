from flask import Flask, request, redirect, url_for, Response, render_template
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
UPLOAD_FOLDER = "models"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

#############################################
# Helper Function: Determine Next Version   #
#############################################

def get_next_version(app_name):
    """
    Determines the next version tag for the given app/model.
    If the model is new (i.e. no existing release folder), returns "v1.0".
    Otherwise, finds the highest version number among the existing releases and returns v(n+1).0.
    """
    app_root = os.path.join(os.getcwd(), "versioned_models", app_name)
    release_dir = os.path.join(app_root, "release")
    if not os.path.exists(release_dir):
        return "v1.0"
    versions = []
    for entry in os.listdir(release_dir):
        if entry.startswith("v"):
            try:
                # Assume tag format is v<number>.0
                number = int(entry[1:].split(".")[0])
                versions.append(number)
            except ValueError:
                continue
    if not versions:
        return "v1.0"
    new_version_number = max(versions) + 1
    return f"v{new_version_number}.0"

#############################################
# Tag/Versioning Function (integrated)      #
#############################################

def tag_version_release(app_name, zip_path, tag, commit_message):
    """
    Extracts the ZIP file (which should contain app.py, requirements.txt, and a model file),
    then moves files into a versioned folder structure and tags the release in a Git repository.
    """
    full_tag = f"{app_name}_{tag}"  # e.g., ocr_v2.0

    # Extract the ZIP file to a temporary directory
    temp_dir = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
    except zipfile.BadZipFile:
        print(f"Error: The file '{zip_path}' is not a valid zip file.")
        return False

    # Identify expected files: app.py, requirements.txt, and a PyTorch model file (.pt or .pth)
    extracted_files = os.listdir(temp_dir)
    code_files = []
    model_file = None

    for filename in extracted_files:
        if filename in ["app.py", "requirements.txt"]:
            code_files.append(filename)
        elif filename.endswith(".pt") or filename.endswith(".pth"):
            model_file = filename

    if "app.py" not in code_files or "requirements.txt" not in code_files:
        print("Error: app.py and/or requirements.txt not found in the zip file.")
        shutil.rmtree(temp_dir)
        return False
    if model_file is None:
        print("Error: No PyTorch model file (.pt or .pth) found in the zip file.")
        shutil.rmtree(temp_dir)
        return False

    # Define destination directories for versioning
    app_root = os.path.join(os.getcwd(), "versioned_models", app_name)
    release_dir = os.path.join(app_root, "release", tag)
    models_dir = os.path.join(app_root, "models")

    os.makedirs(app_root, exist_ok=True)
    os.makedirs(release_dir, exist_ok=True)
    os.makedirs(models_dir, exist_ok=True)

    # Move code files (app.py and requirements.txt) to the release directory
    for file in code_files:
        src = os.path.join(temp_dir, file)
        dst = os.path.join(release_dir, file)
        shutil.move(src, dst)

    # Move the model file to the models directory
    src_model = os.path.join(temp_dir, model_file)
    dst_model = os.path.join(models_dir, model_file)
    shutil.move(src_model, dst_model)

    # Clean up the temporary directory
    shutil.rmtree(temp_dir)

    # If the app_root is not already a Git repository, initialize it
    if not os.path.isdir(os.path.join(app_root, ".git")):
        try:
            subprocess.run(["git", "init", app_root], check=True)
            print(f"Initialized a new Git repository in {app_root}")
        except subprocess.CalledProcessError:
            print("❌ Git init failed. Ensure Git is installed.")
            return False

    # Change working directory to the app root for subsequent Git commands
    current_dir = os.getcwd()
    os.chdir(app_root)

    # Initialize Git LFS (make sure Git LFS is installed on your system)
    try:
        subprocess.run(["git", "lfs", "install"], check=True)
    except subprocess.CalledProcessError:
        print("❌ Git LFS installation failed. Make sure Git LFS is installed on your system.")
        os.chdir(current_dir)
        return False

    # Track the model file with Git LFS using its relative path
    lfs_track_path = os.path.join("models", model_file)
    try:
        subprocess.run(["git", "lfs", "track", lfs_track_path], check=True)
    except subprocess.CalledProcessError:
        print("❌ Git LFS tracking failed. Ensure the command is correct.")
        os.chdir(current_dir)
        return False

    # Stage all changes in the repository
    try:
        subprocess.run(["git", "add", "."], check=True)
    except subprocess.CalledProcessError:
        print("❌ Git add failed. Ensure you are in the correct repository.")
        os.chdir(current_dir)
        return False

    # Commit the changes with the provided commit message
    try:
        subprocess.run(["git", "commit", "-m", f"[{app_name}] {commit_message}"], check=True)
    except subprocess.CalledProcessError:
        print("❌ Git commit failed. Ensure there are changes to commit and your Git user is configured.")
        os.chdir(current_dir)
        return False

    # Create a new Git tag for this release
    try:
        subprocess.run(["git", "tag", full_tag], check=True)
        print(f"✅ Git tag '{full_tag}' created for the new release.")
    except subprocess.CalledProcessError:
        print("❌ Git tagging failed. The tag might already exist or be invalid.")
        os.chdir(current_dir)
        return False

    # (Optional) Push the commit and tags to a remote repository:
    # subprocess.run(["git", "push"], check=True)
    # subprocess.run(["git", "push", "--tags"], check=True)

    os.chdir(current_dir)
    return True

#############################################
# Deployment Function                       #
#############################################

def deploy_model(model_name, zip_path, descriptor):
    """
    Simulates the lifecycle manager to provision a container and deploy the model.
    It extracts the zip into a deployment folder, creates a virtual environment,
    installs dependencies, and returns a port number on which the model will run.
    """
    print(f"Deploying model {model_name}...")

    deployed_dir = os.path.join("deployed_models")
    os.makedirs(deployed_dir, exist_ok=True)

    # Find an available port dynamically.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', 0))
    port_no = s.getsockname()[1]
    s.close()

    # Create model-specific deployment directory using the port number.
    model_deploy_dir = os.path.join(deployed_dir, f"{port_no}")
    os.makedirs(model_deploy_dir, exist_ok=True)

    # Extract the zip contents to the deployment directory.
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(model_deploy_dir)

    deployed_descriptor_path = os.path.join(model_deploy_dir, "descriptor.json")
    descriptor_data = descriptor.copy()
    descriptor_data["deployed_at"] = datetime.datetime.now().isoformat()
    descriptor_data["status"] = "deployed"
    descriptor_data["port"] = port_no

    with open(deployed_descriptor_path, 'w') as dest_file:
        json.dump(descriptor_data, dest_file, indent=4)

    # Create a virtual environment in the deployment directory.
    venv_path = os.path.join(model_deploy_dir, "venv")
    subprocess.run(["python", "-m", "venv", venv_path])
    pip_command = os.path.join(venv_path, "bin" if os.name != "nt" else "Scripts", "pip")
    subprocess.run([pip_command, "install", "--upgrade", "pip"])

    # Install each dependency listed in the descriptor.
    for dependency in descriptor_data["requirements"]:
        subprocess.run([pip_command, "install", dependency])

    app.logger.info(f"Model {model_name} deployed on port {port_no}")
    return port_no

#############################################
# Flask Routes                              #
#############################################

# Home page: links to list models and model upload form
@app.route("/")
def index():
    return render_template("index.html")

# Endpoint to handle model uploads
@app.route("/upload", methods=["POST"])
def upload_model():
    required_files = ["model_definition", "model_weights", "requirements"]
    for file_key in required_files:
        if file_key not in request.files:
            return f"No file provided for {file_key}", 400

    model_name = request.form.get("model_name", "").strip()
    if not model_name:
        return "Model name is required", 400

    model_def_file = request.files["model_definition"]
    weights_file = request.files["model_weights"]
    req_file = request.files["requirements"]

    if model_def_file.filename == "" or weights_file.filename == "" or req_file.filename == "":
        return "One or more files were not selected", 400

    # Create folder for the model upload and save the files
    model_folder = os.path.join(UPLOAD_FOLDER, secure_filename(model_name))
    os.makedirs(model_folder, exist_ok=True)

    model_def_path = os.path.join(model_folder, "app.py")
    weights_filename = secure_filename(weights_file.filename)
    weights_path = os.path.join(model_folder, weights_filename)
    req_path = os.path.join(model_folder, "requirements.txt")

    # Process the requirements file into a list
    requirements = req_file.read().decode('utf-8').splitlines()
    requirements_list = [re.sub(r"==.*", "", req.strip()) for req in requirements if req.strip()]
    descriptor = {
        "model_name": model_name,
        "files": {
            "model_definition": model_def_path,
            "model_weights": weights_path,
        },
        "requirements": requirements_list,
        "version": "auto",  # Version will be determined automatically
        "interface_type": request.form.get("interface_type", "gradio"),
    }

    # Save the uploaded files to disk.
    model_def_file.save(model_def_path)
    weights_file.save(weights_path)
    req_file.save(req_path)

    # Create a ZIP archive of the model artifacts.
    artifacts_dir = os.path.join(UPLOAD_FOLDER, model_name)
    os.makedirs(artifacts_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_filename = f"{secure_filename(model_name)}_{timestamp}.zip"
    zip_path = os.path.join(artifacts_dir, zip_filename)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        zipf.write(model_def_path, arcname=os.path.basename(model_def_path))
        zipf.write(weights_path, arcname=weights_filename)
        zipf.write(req_path, arcname=os.path.basename(req_path))

    # Automatically determine version tag.
    version_tag = get_next_version(model_name)
    commit_message = f"Release version {version_tag} for {model_name}"

    tagging_success = tag_version_release(model_name, zip_path, version_tag, commit_message)
    if not tagging_success:
        return "Tagging and versioning failed", 500

    # Deploy the model and update the descriptor with the deployment port.
    port_no = deploy_model(model_name, zip_path, descriptor)
    if port_no is None:
        return "Model deployment failed", 500
    descriptor["port"] = port_no

    # Save the descriptor in the artifacts directory.
    descriptor_path = os.path.join(artifacts_dir, "descriptor.json")
    with open(descriptor_path, 'w') as f:
        json.dump(descriptor, f, indent=4)

    return redirect(url_for("list_models"))

# Endpoint to list available models
@app.route("/models", methods=["GET"])
def list_models():
    models = os.listdir(UPLOAD_FOLDER)
    return render_template("models.html", models=models)

gradio_servers = {}

@app.route("/model/<model_name>", methods=["GET"])
def model_specific(model_name):
    if model_name not in os.listdir(UPLOAD_FOLDER):
        return "Model not found", 404

    # Load model descriptor
    descriptor_path = os.path.join(UPLOAD_FOLDER, model_name, "descriptor.json")
    if not os.path.exists(descriptor_path):
        return f"Descriptor file for model {model_name} not found", 404

    with open(descriptor_path, 'r') as f:
        descriptor = json.load(f)

    interface_type = descriptor.get("interface_type", "gradio")
    port_no = descriptor.get("port", None)
    if port_no is None:
        return f"Model {model_name} is not properly deployed (no port defined)", 400

    if interface_type != "gradio":
        return f"Interface type {interface_type} is not supported", 400

    if model_name in gradio_servers:
        port = gradio_servers[model_name]["port"]
        return render_template("model_interface.html", model_name=model_name, port=port)

    model_folder = os.path.join("deployed_models", str(port_no))
    if not os.path.exists(model_folder):
        return f"Deployment folder for model {model_name} not found", 404

    model_file = os.path.join(model_folder, "app.py")
    print(f"Launching model server for {model_name}...")
    print(f"Model file: {model_file}")

    env = os.environ.copy()
    env["GRADIO_SERVER_PORT"] = str(port_no)
    env["GRADIO_ROOT_PATH"] = f"/model/{model_name}"

    try:
        process = subprocess.Popen(
            ["python", "app.py"],
            cwd=model_folder,
            env=env,
        )
        gradio_servers[model_name] = {"process": process, "port": port_no}
    except Exception as e:
        return f"Failed to start model server: {str(e)}", 500

    return render_template("model_interface.html", model_name=model_name, port=port_no)

# API Proxy for forwarding requests to the model server
@app.route("/model/<model_name>/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def proxy_model_api(model_name, subpath):
    if model_name not in gradio_servers:
        return f"Model {model_name} is not running", 404

    port = gradio_servers[model_name]["port"]
    target_url = f"http://localhost:{port}/{subpath}"
    params = dict(request.args)
    if "session_hash" not in params:
        params["session_hash"] = "1234"
    print(f"Proxying request for {model_name} to {target_url} with params {params}")

    resp = requests.request(
        method=request.method,
        url=target_url,
        params=params,
        headers={key: value for key, value in request.headers if key != "Host"},
        data=request.get_data(),
        cookies=request.cookies,
        allow_redirects=False
    )
    excluded_headers = ["content-encoding", "content-length", "transfer-encoding", "connection"]
    headers = [(name, value) for name, value in resp.raw.headers.items() if name.lower() not in excluded_headers]
    return Response(resp.content, resp.status_code, headers)

# Endpoint to display API documentation for the model
@app.route("/model/<model_name>/api_doc")
def api_doc_model(model_name):
    if model_name not in os.listdir(UPLOAD_FOLDER):
        return "Model not found", 404
    return render_template("api_doc.html", model_name=model_name)

# Endpoint to display running instances for a model
@app.route("/model/<model_name>/instances")
def instances_model(model_name):
    count = 1 if model_name in gradio_servers else 0
    return render_template("instances.html", model_name=model_name, count=count)

if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False)
