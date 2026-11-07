"""Virtual Creator Office — ruang kerja otonom 6 Super Bot berbasis Microsoft AutoGen.

Modul ini sengaja dipisah dari ``virtual_office_superbots.py`` supaya:

* tool nyata (filesystem, ffmpeg, YouTube) bisa diuji tanpa memanggil LLM;
* konfigurasi persona/tool/routing terbaca sebagai data, bukan tersebar di script;
* entry point CLI tetap satu file tipis yang mudah dijalankan.
"""

from .config import Settings, StudioPaths
from .daemon import Daemon, Job
from .memory import MemoryStore
from .office import Office, build_office, kickoff_message

__all__ = [
    "Daemon",
    "Job",
    "MemoryStore",
    "Office",
    "Settings",
    "StudioPaths",
    "build_office",
    "kickoff_message",
]
__version__ = "2.1.0"
