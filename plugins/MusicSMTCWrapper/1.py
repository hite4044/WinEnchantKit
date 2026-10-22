from time import sleep
from typing import TypeVar

from winsdk.windows.foundation import AsyncStatus, IAsyncOperation
from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as GlobalSMTCManager
from winsdk.windows.storage import FileIO, KnownFolders, KnownFolderId, CreationCollisionOption
from winsdk.windows.storage.streams import DataReader

T = TypeVar('T')


def wait_result(opt: IAsyncOperation[T]) -> T:
    while opt.status != AsyncStatus.COMPLETED:
        sleep(0.1)
    return opt.get_results()


manager = wait_result(GlobalSMTCManager.request_async())
session = manager.get_current_session()

# 展示基本信息
print("SMTC Id:", session.source_app_user_model_id)
media_prop = wait_result(session.try_get_media_properties_async())
print("SMTC Title:", media_prop.title)
print("SMTC Artist:", media_prop.artist)
while True:
    sleep(1)
    timeline = session.get_timeline_properties()
    print(f"SMTC Position: {timeline.start_time} - {timeline.end_time} # {timeline.position}")

# 导出封面为图片
stream = wait_result(media_prop.thumbnail.open_read_async())
data_reader = DataReader(stream.get_input_stream_at(0))
wait_result(data_reader.load_async(stream.size))
buffer = data_reader.read_buffer(stream.size)
downloads_folder = wait_result(KnownFolders.get_folder_async(KnownFolderId.DOWNLOADS_FOLDER))
output_file = wait_result(downloads_folder.create_file_async("thumbnail.png", CreationCollisionOption.REPLACE_EXISTING))
FileIO.write_buffer_async(output_file, buffer)

from PIL import Image
image = Image.open(r"C:\Users\hite404\Downloads\thumbnail.png")
print("Size of the thumbnail:", image.size)
image.show()
