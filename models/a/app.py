import torch
import torch.nn as nn
import torchvision.transforms as transforms
from PIL import Image
import gradio as gr

# Define the model architecture
class MNISTModel(nn.Module):
    def __init__(self):
        super(MNISTModel, self).__init__()
        self.conv_layer = nn.Sequential(
            nn.Conv2d(1, 32, kernel_size=3, stride=1, padding=1),
            nn.ReLU(),
            nn.MaxPool2d(2),
            nn.Conv2d(32, 64, kernel_size=3, stride=1, padding=1),
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

# Setup device and load the trained model
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model = MNISTModel().to(device)
model.load_state_dict(torch.load("mnist_cnn.pth", map_location=device))
model.eval()

# Define the image transformation pipeline (should match training)
transform = transforms.Compose([
    transforms.Grayscale(num_output_channels=1),
    transforms.Resize((28, 28)),
    transforms.ToTensor(),
    transforms.Normalize((0.1307,), (0.3081,))
])

# Prediction function for Gradio
def predict_digit(image):
    try:
        # image is a PIL Image if using type="pil"
        image = transform(image).unsqueeze(0).to(device)
    except Exception as e:
        return f"Error processing image: {e}"
    
    with torch.no_grad():
        outputs = model(image)
        _, predicted = torch.max(outputs, 1)
    return f"Predicted Digit: {predicted.item()}"


iface = gr.Interface(
    inputs=gr.Image(type="pil", label="Draw a digit"),
    outputs=gr.Textbox(label="Prediction"),
    fn=predict_digit,
    title="MNIST Digit Prediction",
)

if __name__ == "__main__":
    iface.launch(show_api=False)
