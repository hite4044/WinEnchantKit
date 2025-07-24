import wx
from PIL import Image

from lib.image_pil2wx import PilImg2WxImg


def set_multi_size_icon(frame: wx.TopLevelWindow, icon_path: str,
                        resampling: Image.Resampling = Image.Resampling.NEAREST):
    image = Image.open(icon_path)
    size_list = [16, 24, 32, 64, 128, 256, 512]
    bundle = wx.IconBundle()
    for size in size_list:
        sized_image = image.resize((size, size), resampling)
        bitmap = PilImg2WxImg(sized_image).ConvertToBitmap()
        icon = wx.Icon(bitmap)
        bundle.AddIcon(icon)
    frame.SetIcons(bundle)
