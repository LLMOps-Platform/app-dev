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
# Global Gradio Server Structure            #
#############################################
# New structure: { model_name: { "running_instances": [ { "port": int, "process": Process or None } ],
#                                 "deploying": Boolean,
#                                 "deploy_instance": { "port": int, "process": Process or None } or None } }
gradio_servers = {}

#############################################
# Deployment Function                       #
#############################################

# Kafka Config
KAFKA_BROKER = "10.1.37.28:9092"
KAFKA_TOPIC = "logs"

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

def deploy_model(model_name, zip_path, descriptor):
    """
    Deploys a model by extracting its ZIP package, setting up a virtual environment, installing dependencies,
    and starting the model as a Gradio application.

    Args:
        model_name (str): The name of the model to be deployed.
        zip_path (str): The file path to the ZIP file containing the deployment package.
        descriptor (dict): A dictionary containing metadata and configuration for the model deployment.

    Returns:
        int: The port number on which the model is deployed.
    """
    import datetime
    print(f"Deploying model {model_name}...")
    deployed_dir = os.path.join("deployed_models")
    os.makedirs(deployed_dir, exist_ok=True)
    deployed_dir = os.path.abspath(deployed_dir)
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
    python_executable = os.path.join(venv_path, "bin", "python") if os.name != "nt" else os.path.join(venv_path, "Scripts", "python")
    cmd = [python_executable, "app.py"]
    process = subprocess.Popen(cmd, env=env, cwd=model_deploy_dir, start_new_session=True)
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

def package_model(model_name, model_def_file, weights_file, req_file):
    """
    Packages raw model files into a release ZIP file and creates a descriptor JSON file.

    Args:
        model_name (str): The name of the model to be packaged.
        model_def_file (FileStorage): The uploaded file object for the model definition (e.g., app.py).
        weights_file (FileStorage): The uploaded file object for the model weights (e.g., .pth file).
        req_file (FileStorage): The uploaded file object for the requirements.txt file.

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
    Handles the upload of model files, packages them into a release ZIP, and deploys the model.

    Args:
        None (uses Flask's request object to access form data and uploaded files).

    Returns:
        Response: A redirect to the list of models if successful, or an error message with an appropriate HTTP status code.
    """
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
    Displays the interface for a specific model, deploying it if necessary.

    Args:
        model_name (str): The name of the model to be displayed or deployed.

    Returns:
        Response: The rendered HTML template for the model interface, or an error message if the model is not found.
    """
    if model_name not in os.listdir(UPLOAD_FOLDER):
        return "Model not found", 404

    descriptor_path = os.path.join(UPLOAD_FOLDER, model_name, "release/descriptor.json")
    if not os.path.exists(descriptor_path):
        return f"Descriptor file for model {model_name} not found", 404
    with open(descriptor_path, 'r') as f:
        descriptor = json.load(f)

    # If there is already a running instance, return it.
    if model_name in gradio_servers and gradio_servers[model_name]["running_instances"]:
        instance = random.choice(gradio_servers[model_name]["running_instances"])
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


@app.route("/model/<model_name>/api_doc")
def api_doc_model(model_name):
    """
    Displays the API documentation for a specific model, including dynamically fetched details from running instances.

    Args:
        model_name (str): The name of the model for which API documentation is to be displayed.

    Returns:
        Response: The rendered HTML template for the API documentation.
    """
    import json

    # Check that the model exists
    if model_name not in os.listdir(UPLOAD_FOLDER):
        return "Model not found", 404

    # Determine the descriptor file path.
    descriptor_path = os.path.join(UPLOAD_FOLDER, model_name, "release/descriptor.json")
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

    # Retrieve API definition from one of the running Gradio instances.
    detailed_api_docs = None
    if model_name in gradio_servers and gradio_servers[model_name]["running_instances"]:
        instance = gradio_servers[model_name]["running_instances"][0]
        port = instance.get("port")
        try:
            # Use the /config endpoint which is typically available in Gradio apps.
            response = requests.get(f"http://127.0.0.1:{port}/gradio_api/info", timeout=5)
            response.raise_for_status()
            detailed_api_docs = response.json()
        except Exception as e:
            detailed_api_docs = f"Error fetching API definition from instance at port {port}: {e}"
    else:
        detailed_api_docs = "No running instances available for API definition."

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
    Displays the running instances of a specific model, including deployment logs and metadata.

    Args:
        model_name (str): The name of the model for which instances are to be displayed.

    Returns:
        Response: The rendered HTML template listing the running instances of the model.
    """
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


@app.route("/model/<model_name>/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def proxy_model_api(model_name, subpath):
    """
    Proxies API requests to a running instance of the specified model.

    Args:
        model_name (str): The name of the model to which the API request is to be proxied.
        subpath (str): The subpath of the API endpoint being accessed.

    Returns:
        Response: The proxied response from the model's API.
    """
    # Choose the first running instance.
    if model_name not in gradio_servers:
        zip_path = os.path.join(UPLOAD_FOLDER, model_name, "release", f"{model_name}.zip")
        descriptor_path = os.path.join(UPLOAD_FOLDER, model_name, "release", "descriptor.json")
        with open(descriptor_path, 'r') as f:
            descriptor = json.load(f)
        deploy_model(model_name, zip_path, descriptor)

    while not gradio_servers[model_name]["running_instances"]:
        time.sleep(0.1)

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
