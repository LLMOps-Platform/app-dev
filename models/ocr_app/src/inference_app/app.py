import io
import base64
import torch
import torch.nn as nn
import torchvision.transforms as transforms
from PIL import Image
from flask import Flask, request, render_template, jsonify

# Define the CNN architecture (same as in training)
class MNIST_CNN(nn.Module):
    def __init__(self):
        super(MNIST_CNN, self).__init__()
        self.conv_layer = nn.Sequential(
            nn.Conv2d(1, 32, kernel_size=3, padding=1),  # input channel 1, output channel 32
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2)
        )
        self.fc_layer = nn.Sequential(
            nn.Linear(64 * 7 * 7, 128),
            nn.ReLU(),
            nn.Linear(128, 10)
        )

    def forward(self, x):
        x = self.conv_layer(x)
        x = x.view(x.size(0), -1)
        x = self.fc_layer(x)
        return x

# Initialize the Flask app
app = Flask(__name__)

# Set up device: use GPU if available, otherwise CPU
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Initialize and load the model
model = MNIST_CNN().to(device)
model_state_path = "mnist_cnn.pt"
try:
    model.load_state_dict(torch.load(model_state_path, map_location=device))
    model.eval()  # set to evaluation mode
    print(f"Model loaded successfully from {model_state_path}")
except Exception as e:
    print(f"Error loading model from {model_state_path}: {e}")
    model = None

# Define the image transformations (same as used during training)
transform = transforms.Compose([
    transforms.Grayscale(num_output_channels=1),  # ensure the image is grayscale
    transforms.Resize((28, 28)),                    # resize to MNIST dimensions
    transforms.ToTensor(),
    transforms.Normalize((0.1307,), (0.3081,))
])

@app.route('/')
def index():
    # Render an HTML page for uploading images.
    # Make sure an index.html exists in a folder named "templates" alongside this file.
    return render_template('index.html')

@app.route('/predict', methods=['POST'])
def predict():
    if model is None:
        return jsonify({'error': 'Model not loaded.'}), 500

    # Check if the request is JSON (sent by the web app) or a file upload
    if request.is_json:
        data = request.get_json()
        image_data = data.get("image_data")
        if not image_data:
            return jsonify({'error': 'No image_data provided in JSON payload'}), 400
        try:
            # image_data is a data URL (e.g., "data:image/png;base64,....")
            header, encoded = image_data.split(",", 1)
            img_bytes = base64.b64decode(encoded)
        except Exception as e:
            return jsonify({'error': f'Error decoding base64 image data: {e}'}), 400
    else:
        if 'image' not in request.files:
            return jsonify({'error': 'No file part in the request'}), 400
        file = request.files['image']
        if file.filename == '':
            return jsonify({'error': 'No file selected for uploading'}), 400
        img_bytes = file.read()

    try:
        # Open the image from bytes and convert it to grayscale
        img = Image.open(io.BytesIO(img_bytes)).convert('L')
        # Preprocess the image
        img = transform(img)
        img = img.unsqueeze(0)  # add a batch dimension
    except Exception as e:
        return jsonify({'error': f'Error processing image: {e}'}), 500

    try:
        # Run the model inference
        with torch.no_grad():
            outputs = model(img.to(device))
            _, predicted = torch.max(outputs.data, 1)
        # Return the prediction as JSON
        return jsonify({'prediction': int(predicted.item())})
    except Exception as e:
        return jsonify({'error': f'Model prediction error: {e}'}), 500

if __name__ == '__main__':
    app.run(debug=True, port=8000)
