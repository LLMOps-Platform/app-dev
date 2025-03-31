import torch
import torch.nn as nn
import torchvision.transforms as transforms
from PIL import Image
import gradio as gr

# # Create Gradio interface
def greet(name):
    return "Hello " + name + "!"

iface = gr.Interface(
    inputs=gr.Textbox(label="Enter your name"),
    outputs=gr.Textbox(label="Greeting"),
    fn=greet,
    title="Greeting App",
)

if __name__ == "__main__":
    iface.launch(show_api=False)
