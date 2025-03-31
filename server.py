import random
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



#############################################
# Global Gradio Server Structure            #
#############################################
# New structure: { model_name: { "running_instances": [ { "port": int, "process": Process or None } ],
#                                 "deploying": Boolean,
#                                 "deploy_instance": { "port": int, "process": Process or None } or None } }
gradio_servers = {}

#############################################
# Deployment Function                       #
#############################################

def log_message(log_file_path, message):
    """Append a log message with a timestamp to the log file."""
    timestamp = datetime.datetime.now().isoformat()
    with open(log_file_path, "a") as lf:
        lf.write(f"[{timestamp}] {message}\n")

def deploy_model(model_name, zip_path, descriptor):
    import datetime
    print(f"Deploying model {model_name}...")
    deployed_dir = os.path.join("deployed_models")
    os.makedirs(deployed_dir, exist_ok=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        port_no = s.getsockname()[1]
    model_deploy_dir = os.path.join(deployed_dir, f"{port_no}")
    os.makedirs(model_deploy_dir, exist_ok=True)

    gradio_servers.setdefault(model_name, {
         "running_instances": [],
         "deploying": False,
         "deploy_instance": None
    })
    gradio_servers[model_name]["deploying"] = True
    gradio_servers[model_name]["deploy_instance"] = {"port": port_no}

    log_file_path = os.path.join(model_deploy_dir, "instance.log")
    log_message(log_file_path, f"Starting deployment of model '{model_name}' on port {port_no}.")
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
         zip_ref.extractall(model_deploy_dir)
    log_message(log_file_path, "Extracted deployment package.")

    deployed_descriptor_path = os.path.join(model_deploy_dir, "descriptor.json")
    descriptor_data = descriptor.copy()
    if "model_name" not in descriptor_data:
         descriptor_data["model_name"] = model_name
    descriptor_data["deployed_at"] = datetime.datetime.now().isoformat()

    # Define env once.
    env = os.environ.copy()
    if descriptor.get("interface_type", "gradio") == "gradio":
         env["GRADIO_SERVER_PORT"] = str(port_no)
         env["GRADIO_ROOT_PATH"] = "/model/" + model_name

    with open(deployed_descriptor_path, "w") as dest_file:
         json.dump(descriptor_data, dest_file, indent=4)
    log_message(log_file_path, "Deployment descriptor updated.")

    venv_path = os.path.join(model_deploy_dir, "venv")
    log_message(log_file_path, "Creating virtual environment.")
    subprocess.run(["python", "-m", "venv", venv_path], env=env, check=True)
    log_message(log_file_path, "Virtual environment created.")

    pip_command = os.path.join(venv_path, "bin", "pip") if os.name != "nt" else os.path.join(venv_path, "Scripts", "pip")
    subprocess.run([pip_command, "install", "--upgrade", "pip"], check=True)
    log_message(log_file_path, "Pip upgraded.")

    for dependency in descriptor_data["requirements"]:
         subprocess.run([pip_command, "install", dependency], check=True)
         log_message(log_file_path, f"Installed dependency: {dependency}")

    # Use Popen instead of run to make it non-blocking
    python_executable = os.path.join(venv_path, 'bin', 'python') if os.name != "nt" else os.path.join(venv_path, 'Scripts', 'python')
    cmd = [python_executable, os.path.join(model_deploy_dir, 'app.py')]
    process = subprocess.Popen(cmd, env=env)
    app.logger.info(f"Model {model_name} deployed on port {port_no}")
    log_message(log_file_path, f"Deployment started successfully on port {port_no}.")

    gradio_servers[model_name]["deploying"] = False
    gradio_servers[model_name]["deploy_instance"] = None
    gradio_servers[model_name].setdefault("running_instances", []).append({
         "port": port_no,
         "process": process
    })
    return port_no


#############################################
# Packaging Function                       #
#############################################

# In server.py – update the package_model function to initialize instance_ports

def package_model(model_name, model_def_file, weights_file, req_file):
    """
    Package the raw model files into a release zip and create a descriptor.
    Returns (descriptor, zip_path).
    """
    from datetime import datetime
    import json
    model_folder = os.path.join(UPLOAD_FOLDER, secure_filename(model_name))
    release_folder = os.path.join(model_folder, "release")
    src_folder = os.path.join(model_folder, "src")
    os.makedirs(release_folder, exist_ok=True)
    os.makedirs(src_folder, exist_ok=True)

    # Save raw files directly in source folder
    model_def_path = os.path.join(src_folder, "app.py")
    weights_path = os.path.join(src_folder, secure_filename(weights_file.filename))
    req_path = os.path.join(src_folder, "requirements.txt")
    model_def_file.save(model_def_path)
    weights_file.save(weights_path)
    req_file.save(req_path)

    # Build descriptor using requirements read from file object
    req_file.seek(0)
    requirements = req_file.read().decode('utf-8').splitlines()
    requirements_list = [re.sub(r"==.*", "", req.strip()) for req in requirements if req.strip()]
    descriptor = {
        "model_name": model_name,
        "files": {
            "model_definition": "app.py",
            "model_weights": secure_filename(weights_file.filename),
        },
        "requirements": requirements_list,
        "version": request.form.get("version", "1.0"),
        "interface_type": request.form.get("interface_type", "gradio"),
        "instance_ports": []  # Initialize empty list for multiple ports
    }

    # Save descriptor in release folder
    descriptor_path = os.path.join(release_folder, "descriptor.json")
    with open(descriptor_path, 'w') as f:
        json.dump(descriptor, f, indent=4)

    # Create zip package containing files directly (not relative to release folder)
    zip_filename = f"{secure_filename(model_name)}.zip"
    zip_path = os.path.join(release_folder, zip_filename)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        # Add app.py
        zipf.write(os.path.join(src_folder, "app.py"), arcname="app.py")
        # Add weights file
        zipf.write(os.path.join(src_folder, secure_filename(weights_file.filename)), 
                  arcname=secure_filename(weights_file.filename))
        # Add requirements file
        zipf.write(os.path.join(src_folder, "requirements.txt"), arcname="requirements.txt")
        
    return descriptor, zip_path
#############################################
# Flask Routes                              #
#############################################

# Home page: links to list models and model upload form
@app.route("/")
def index():
    return render_template("index.html")

# Endpoint to handle model uploads
# In server.py – update the /upload route to append the deployed port

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

    # Update the release descriptor to track multiple instance ports
    release_folder = os.path.join(UPLOAD_FOLDER, secure_filename(model_name), "release")
    descriptor_path = os.path.join(release_folder, "descriptor.json")
    # Load the existing descriptor, update instance_ports and save
    with open(descriptor_path, 'r') as f:
        descriptor_data = json.load(f)
    if "instance_ports" not in descriptor_data:
        descriptor_data["instance_ports"] = []
    descriptor_data["instance_ports"].append(port_no)

    with open(descriptor_path, 'w') as f:
        json.dump(descriptor_data, f, indent=4)

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

    descriptor_path = os.path.join(UPLOAD_FOLDER, model_name, "release/descriptor.json")
    if not os.path.exists(descriptor_path):
        return f"Descriptor file for model {model_name} not found", 404
    with open(descriptor_path, 'r') as f:
        descriptor = json.load(f)

    # If there is already a running instance, return it.
    if model_name in gradio_servers and gradio_servers[model_name]["running_instances"]:
        instance = gradio_servers[model_name]["running_instances"][random.randint(0,len(gradio_servers[model_name]["running_instances"]))]
        return render_template("model_interface.html", model_name=model_name, port=instance["port"])

    # Otherwise, deploy the model using deploy_models.
    if model_name not in gradio_servers or (gradio_servers[model_name]["deploying"] == False):
        import glob
        release_folder = os.path.join(UPLOAD_FOLDER, model_name, "release")
        zip_files = glob.glob(os.path.join(release_folder, "*.zip"))
        if not zip_files:
            return "No deployment package found", 404
        zip_path = zip_files[0]
        port = deploy_model(model_name, zip_path, descriptor)
        return render_template("model_interface.html", model_name=model_name, port=port)
    
    return redirect(url_for("instances_model", model_name=model_name))



# Endpoint to display API documentation for the model
@app.route("/model/<model_name>/api_doc")
def api_doc_model(model_name):
    import json

    # Check that the model folder exists
    if (model_name not in os.listdir(UPLOAD_FOLDER)):
        return "Model not found", 404

    # Determine the descriptor file path.
    # Adjust the location if your descriptor is stored in a subfolder (e.g. release)
    descriptor_path = os.path.join(UPLOAD_FOLDER, model_name, "release/descriptor.json")
    if not os.path.exists(descriptor_path):
        return f"Descriptor file for model {model_name} not found", 404

    with open(descriptor_path, "r") as f:
        descriptor = json.load(f)

    # Build API endpoints information dynamically
    api_endpoints = {
        "Model API": url_for("proxy_model_api", model_name=model_name, subpath="", _external=True),
        "API Doc": url_for("api_doc_model", model_name=model_name, _external=True),
        "Instances": url_for("instances_model", model_name=model_name, _external=True)
    }

    # Allow the descriptor to provide additional customizations.
    num_endpoints = descriptor.get("num_api_endpoints", len(api_endpoints))
    code_snippets = descriptor.get("code_snippets", None)

    return render_template(
        "api_doc.html",
        model_name=model_name,
        descriptor=descriptor,
        api_endpoints=api_endpoints,
        num_endpoints=num_endpoints,
        code_snippets=code_snippets
    )


# Endpoint to display running instances for a model
# In server.py – update the instances endpoint

@app.route("/model/<model_name>/instances")
def instances_model(model_name):
    # Get running instances from gradio_servers
    server_info = gradio_servers.get(model_name, {})
    running_instances = server_info.get("running_instances", [])
    deploy_instance = server_info.get("deploy_instance", None)

    instances = []
    deployed_dir = "deployed_models"

    # Build instance info from running instances
    for inst in running_instances:
        port = inst.get("port")
        instance_dir = os.path.join(deployed_dir, str(port))
        deployed_at = "Unknown"
        logs = ""
        instance_descriptor_path = os.path.join(instance_dir, "descriptor.json")
        if os.path.exists(instance_descriptor_path):
            with open(instance_descriptor_path, "r") as f:
                instance_descriptor = json.load(f)
            deployed_at = instance_descriptor.get("deployed_at", "Unknown")
        log_file = os.path.join(instance_dir, "instance.log")
        if os.path.exists(log_file):
            with open(log_file, "r") as lf:
                logs = lf.read()
        instances.append({
            "port": port,
            "deployed_at": deployed_at,
            "logs": logs,
            "running": True
        })

    # Include deploy_instance info if available and not already listed
    if deploy_instance:
        port = deploy_instance.get("port")
        if not any(inst.get("port") == port for inst in instances):
            instance_dir = os.path.join(deployed_dir, str(port))
            deployed_at = "Unknown"
            logs = ""
            instance_descriptor_path = os.path.join(instance_dir, "descriptor.json")
            if os.path.exists(instance_descriptor_path):
                with open(instance_descriptor_path, "r") as f:
                    instance_descriptor = json.load(f)
                deployed_at = instance_descriptor.get("deployed_at", "Unknown")
            log_file = os.path.join(instance_dir, "instance.log")
            if os.path.exists(log_file):
                with open(log_file, "r") as lf:
                    logs = lf.read()
            instances.append({
                "port": port,
                "deployed_at": deployed_at,
                "logs": logs,
                "running": False
            })

    return render_template("instances.html", model_name=model_name, instances=instances)


# Endpoint to handle model API requests does not get called in frontend
@app.route("/model/<model_name>/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def proxy_model_api(model_name, subpath):

    # Choose the first running instance.
    if model_name not in gradio_servers:
        zip_path = os.path.join(UPLOAD_FOLDER, model_name, "release", f"{model_name}.zip")
        descriptor_path = os.path.join(UPLOAD_FOLDER, model_name, "release", "descriptor.json")
        with open(descriptor_path, 'r') as f:
            descriptor = json.load(f)
        threading.Thread(target=deploy_model, args=(model_name, zip_path, descriptor)).start()
        return f"Model {model_name} is not running. Try calling api after a bit of time. Deployment has started.", 404

    instance = random.choice(gradio_servers[model_name]["running_instances"])
    port = instance["port"]

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

if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False)
