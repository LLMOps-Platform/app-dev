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
    Extracts the ZIP file, moves files into a versioned folder structure, and tags the release in Git.
    """
    full_tag = f"{app_name}_{tag}"  # e.g., ocr_v2.0
    temp_dir = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
    except zipfile.BadZipFile:
        print(f"Error: The file '{zip_path}' is not a valid zip file.")
        return False

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

    app_root = os.path.join(os.getcwd(), "versioned_models", app_name)
    release_dir = os.path.join(app_root, "release", tag)
    models_dir = os.path.join(app_root, "models")
    os.makedirs(app_root, exist_ok=True)
    os.makedirs(release_dir, exist_ok=True)
    os.makedirs(models_dir, exist_ok=True)

    for file in code_files:
        src = os.path.join(temp_dir, file)
        dst = os.path.join(release_dir, file)
        shutil.move(src, dst)
    src_model = os.path.join(temp_dir, model_file)
    dst_model = os.path.join(models_dir, model_file)
    shutil.move(src_model, dst_model)
    shutil.rmtree(temp_dir)

    # Git operations (initialization, LFS, commit, tagging) follow...
    if not os.path.isdir(os.path.join(app_root, ".git")):
        try:
            subprocess.run(["git", "init", app_root], check=True)
            print(f"Initialized a new Git repository in {app_root}")
        except subprocess.CalledProcessError:
            print("❌ Git init failed. Ensure Git is installed.")
            return False

    current_dir = os.getcwd()
    os.chdir(app_root)
    try:
        subprocess.run(["git", "lfs", "install"], check=True)
    except subprocess.CalledProcessError:
        print("❌ Git LFS installation failed.")
        os.chdir(current_dir)
        return False

    lfs_track_path = os.path.join("models", model_file)
    try:
        subprocess.run(["git", "lfs", "track", lfs_track_path], check=True)
    except subprocess.CalledProcessError:
        print("❌ Git LFS tracking failed.")
        os.chdir(current_dir)
        return False

    try:
        subprocess.run(["git", "add", "."], check=True)
    except subprocess.CalledProcessError:
        print("❌ Git add failed.")
        os.chdir(current_dir)
        return False

    try:
        subprocess.run(["git", "commit", "-m", f"[{app_name}] {commit_message}"], check=True)
    except subprocess.CalledProcessError:
        print("❌ Git commit failed.")
        os.chdir(current_dir)
        return False

    try:
        subprocess.run(["git", "tag", full_tag], check=True)
        print(f"✅ Git tag '{full_tag}' created for the new release.")
    except subprocess.CalledProcessError:
        print("❌ Git tagging failed.")
        os.chdir(current_dir)
        return False

    os.chdir(current_dir)
    return True


## Server List
gradio_servers = {}


#############################################
# Deployment Function                       #
#############################################

def deploy_model(model_name, zip_path, descriptor):
    """
    Deploys the packaged model by extracting the zip into deployed_models/<port>,
    creating a virtual environment, installing dependencies, and returning a port number.
    """
    print(f"Deploying model {model_name}...")

    deployed_dir = os.path.join("deployed_models")
    os.makedirs(deployed_dir, exist_ok=True)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', 0))
    port_no = s.getsockname()[1]
    s.close()

    model_deploy_dir = os.path.join(deployed_dir, f"{port_no}")
    os.makedirs(model_deploy_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(model_deploy_dir)

    deployed_descriptor_path = os.path.join(model_deploy_dir, "descriptor.json")
    descriptor_data = descriptor.copy()
    descriptor_data["deployed_at"] = datetime.datetime.now().isoformat()
    descriptor_data["status"] = "deployed"
    descriptor_data["port"] = port_no

    with open(deployed_descriptor_path, 'w') as dest_file:
        json.dump(descriptor_data, dest_file, indent=4)

    venv_path = os.path.join(model_deploy_dir, "venv")
    subprocess.run(["python", "-m", "venv", venv_path])
    pip_command = os.path.join(venv_path, "bin" if os.name != "nt" else "Scripts", "pip")
    subprocess.run([pip_command, "install", "--upgrade", "pip"])
    for dependency in descriptor_data["requirements"]:
        subprocess.run([pip_command, "install", dependency])
    app.logger.info(f"Model {model_name} deployed on port {port_no}")
    return port_no


#############################################
# Packaging Function                       #
#############################################

def package_model(model_name, model_def_file, weights_file, req_file):
    """
    Package the raw model files into a release zip and create a descriptor.
    Returns (descriptor, zip_path).
    """
    from datetime import datetime
    import json
    model_folder = os.path.join(UPLOAD_FOLDER, secure_filename(model_name))
    src_folder = os.path.join(model_folder, "src")
    release_folder = os.path.join(model_folder, "release")
    os.makedirs(src_folder, exist_ok=True)
    os.makedirs(release_folder, exist_ok=True)
    
    # Save raw files in src
    model_def_path = os.path.join(src_folder, "app.py")
    weights_path = os.path.join(src_folder, secure_filename(weights_file.filename))
    req_path = os.path.join(src_folder, "requirements.txt")
    model_def_file.save(model_def_path)
    weights_file.save(weights_path)
    req_file.save(req_path)
    
    # Build descriptor using requirements read from file object
    requirements = req_file.read().decode('utf-8').splitlines()
    requirements_list = [re.sub(r"==.*", "", req.strip()) for req in requirements if req.strip()]
    descriptor = {
        "model_name": model_name,
        "files": {
            "model_definition": "src/app.py",
            "model_weights": f"src/{secure_filename(weights_file.filename)}",
        },
        "requirements": requirements_list,
        "version": request.form.get("version", "1.0"),
        "interface_type": request.form.get("interface_type", "gradio"),
    }
    
    # Save descriptor in release folder
    descriptor_path = os.path.join(release_folder, "descriptor.json")
    with open(descriptor_path, 'w') as f:
        json.dump(descriptor, f, indent=4)
    
    # Create zip package containing descriptor and src
    zip_filename = f"{secure_filename(model_name)}.zip"
    zip_path = os.path.join(release_folder, zip_filename)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        zipf.write(descriptor_path, arcname="descriptor.json")
        for root, _, files in os.walk(src_folder):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, model_folder)
                zipf.write(file_path, arcname=arcname)
    return descriptor, zip_path



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

    descriptor, zip_path = package_model(model_name, model_def_file, weights_file, req_file)
    port_no = deploy_model(model_name, zip_path, descriptor)
    if port_no is None:
        return "Model deployment failed", 500

    descriptor["port"] = port_no
    release_folder = os.path.join(UPLOAD_FOLDER, secure_filename(model_name), "release")
    descriptor_path = os.path.join(release_folder, "descriptor.json")
    with open(descriptor_path, 'w') as f:
        json.dump(descriptor, f, indent=4)

    return redirect(url_for("list_models"))


# Endpoint to list available models
@app.route("/models", methods=["GET"])
def list_models():
    models = os.listdir(UPLOAD_FOLDER)
    return render_template("models.html", models=models)



@app.route("/model/<model_name>", methods=["GET"])
def model_specific(model_name):
    if model_name not in os.listdir(UPLOAD_FOLDER):
        return "Model not found", 404

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

    # If model deployment is not registered, deploy using our deploy_model function only.
    if model_name not in gradio_servers:
        # Locate the release zip package
        release_folder = os.path.join(UPLOAD_FOLDER, model_name, "release")
        import glob
        zip_files = glob.glob(os.path.join(release_folder, "*.zip"))
        if not zip_files:
            return "No deployment package found", 404
        zip_path = zip_files[0]
        new_port = deploy_model(model_name, zip_path, descriptor)
        if new_port is None:
            return "Failed to deploy model", 500
        # Do not launch a server via subprocess; simply record the deployed port.
        gradio_servers[model_name] = {"port": new_port}
        return render_template("model_interface.html", model_name=model_name, port=new_port)
    else:
        port = gradio_servers[model_name]["port"]
        return render_template("model_interface.html", model_name=model_name, port=port)

# Endpoint to handle model API requests does not get called in frontend
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
