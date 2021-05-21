import json
import logging
import os
import pathlib
from re import I
import shlex
import time
import typing

from ipykernel.comm import Comm
from ipykernel.ipkernel import IPythonKernel
from jupyter_client.connect import tunnel_to_kernel
from jupyter_client.ioloop.manager import IOLoopKernelManager
from jupyter_client.kernelspec import NoSuchKernel
from jupyter_client.multikernelmanager import MultiKernelManager
from jupyter_client.threaded import ThreadedKernelClient, ThreadedZMQSocketChannel
from jupyter_core.paths import jupyter_data_dir
from tornado import gen
from traitlets.traitlets import Bool, Type

from .binding import Binding, BindingManager
from .kernelspec import RemoteKernelSpecManager
from .magics import BindingMagics

if typing.TYPE_CHECKING:
    from jupyter_client import KernelClient, KernelManager

LOG = logging.getLogger(__name__)
HYDRA_DATA_DIR = os.path.join(jupyter_data_dir(), "hydra-kernel")

pathlib.Path(HYDRA_DATA_DIR).mkdir(exist_ok=True)

__version__ = "0.0.1"


# Do some subclassing to ensure we are spawning threaded clients
# for our proxy kernels (the default is blocking.)
class HydraChannel(ThreadedZMQSocketChannel):
    def __init__(self, socket, session, loop):
        super(HydraChannel, self).__init__(socket, session, loop)
        self._pipes = []

    def call_handlers(self, msg):
        for handler in self._pipes:
            handler(msg)

    def pipe(self, handler):
        self._pipes.append(handler)

    def unpipe(self):
        self._pipes = []


class HydraKernelClient(ThreadedKernelClient):
    shell_channel_class = Type(HydraChannel)
    iopub_channel_class = Type(HydraChannel)


class HydraKernelManager(IOLoopKernelManager):
    client_class = "hydra_kernel.kernel.HydraKernelClient"

    tunnel = Bool(True, help=(
        "If set, connection to remote kernel will be established over an SSH "
        "tunnel. Remote kernels on loopback hosts will not have tunnels."))

    _binding: Binding = None

    @property
    def needs_tunnel(self):
        return self.tunnel and not self._binding.is_loopback

    def init_binding(self, binding: Binding):
        self._binding = binding
        self.kernel_spec_manager = RemoteKernelSpecManager(binding=binding)

    def pre_start_kernel(self, **kw):
        LOG.debug(f"Looking for kernel in {self.kernel_spec_manager}")
        try:
            self.kernel_spec_manager.get_kernel_spec(self.kernel_name)
        except NoSuchKernel:
            self.kernel_spec_manager.install_kernel_spec(None, kernel_name=self.kernel_name)

        # Actually start the kernel on the remote, it will return the pid
        code, stdout, stderr = self._binding.exec(
            f"hydra spawn {shlex.quote(self.id)} {shlex.quote(self.kernel_name)}"
        )
        res = json.load(stdout)
        # pid, connection
        self.load_connection_info(res["connection"])

        if self.needs_tunnel:
            conn = self._binding.connection
            sshkey = conn.get("ssh_private_key_file")
            sshserver = f"{conn.get('user')}@{conn.get('host')}"
            (
                self.shell_port,
                self.iopub_port,
                self.stdin_port,
                self.hb_port,
                self.control_port
            ) = tunnel_to_kernel(self.get_connection_info(), sshserver, sshkey=sshkey)

        return super().pre_start_kernel(**kw)


class HydraMultiKernelManager(MultiKernelManager):
    kernel_manager_class = "hydra_kernel.kernel.HydraKernelManager"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.connection_dir = HYDRA_DATA_DIR

    def pre_start_kernel(self, kernel_name, kwargs):
        (
            km,
            kernel_name,
            kernel_id
        ) = super().pre_start_kernel(kernel_name, kwargs)
        km.init_binding(kwargs.pop("binding"))
        km.id = kernel_id
        return km, kernel_name, kernel_id


class ProxyComms(object):
    def __init__(self, session, parent, iopub, shells):
        self.session = session
        self.parent = parent
        self.iopub = iopub
        self.shells = shells

        self._reply_content = None
        self._kernel_idle = False

    @property
    def reply_content(self):
        if not self._kernel_idle:
            return None
        return self._reply_content

    def on_iopub_message(self, msg):
        msg_type = msg["header"]["msg_type"]
        content = msg.get("content")
        LOG.info("IOPUB processing %s", msg_type)
        self.session.send(
            self.iopub,
            msg_type,
            content=content,
            parent=self.parent,
            metadata=msg.get("metadata"),
        )

        if msg_type == "status" and content["execution_state"] == "idle":
            LOG.info("IOPUB calling on idle return")
            self._kernel_idle = True

    def on_shell_message(self, msg):
        msg_type = msg["header"]["msg_type"]
        content = msg.get("content")
        LOG.info("SHELL processing %s", msg_type)
        if msg_type == "execute_request":
            # This message was sent ourselves and should not be
            # proxied back to the source.
            return

        for s in self.shells:
            self.session.send(
                s,
                msg_type,
                content=content,
                parent=self.parent,
                metadata=msg.get("metadata"),
            )

        if msg_type == "execute_reply":
            self._reply_content = content


def spawn_kernel(kernel_manager: "MultiKernelManager", binding: "Binding") -> "tuple[KernelManager,KernelClient]":
    kernel_id: "str" = kernel_manager.start_kernel(binding.kernel, binding=binding)
    km: "KernelManager" = kernel_manager.get_kernel(kernel_id)

    try:
        kc: "KernelClient" = km.client()
        # Only connect shell and iopub channels
        kc.start_channels(shell=True, iopub=True, stdin=False, hb=False)
    except RuntimeError:
        km.shutdown_kernel()
        raise

    return km, kc


class HydraKernel(IPythonKernel):
    """
    Hydra Kernel
    """

    log = LOG

    implementation = "hydra_kernel"
    implementation_version = __version__

    language_info = {
        "name": "hydra",
        "codemirror_mode": "python",
        "mimetype": "text/python",
        "file_extension": ".py",
    }

    _kernels: "dict[str,tuple[KernelManager,KernelClient]]" = {}
    _comm: "Comm" = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.binding_manager = BindingManager()
        if self.shell:
            binding_magics = BindingMagics(self.shell, self.binding_manager)
            self.shell.register_magics(binding_magics)

        self.binding_manager.on_change(self.on_binding_change)
        self.kernel_manager = HydraMultiKernelManager()

    def start(self):
        super().start()
        LOG.debug("Registering comm channel")
        self.comm_manager.register_target("banana", self.register_banana)

    def register_banana(self, comm: "Comm", message: "dict"):
        if self._comm:
            self._comm.on_msg(None)
        self._comm = comm
        self._comm.on_msg(self.on_comm_msg)
        LOG.debug(f"Registered comm channel {comm} with open request {message}")

    def on_binding_change(self, binding: "Binding", change: "dict"):
        if self._comm:
            self._comm.send({
                "event": "binding_update",
                "binding": binding.as_dict()
            })

    def on_comm_msg(self, message: "dict"):
        payload = message.get("content", {}).get("data", {})
        LOG.debug(f"Got message: {payload}")
        if payload["event"] == "binding_list_request":
            if self._comm:
                self._comm.send({
                    "event": "binding_list_reply",
                    "bindings": [
                        b.as_dict() for b in self.binding_manager.list()
                    ]
                })

    @property
    def banner(self):
        return "Hydra"

    @gen.coroutine
    def execute_request(self, stream, ident, parent):
        binding_name = parent["metadata"].get("chameleon.binding_name")

        if not binding_name:
            return super(HydraKernel, self).execute_request(stream, ident, parent)

        content = parent["content"]
        silent = content["silent"]
        stop_on_error = content.get("stop_on_error", True)

        # Check if binding name is valid (is there a binding set up?)
        if binding_name not in self._kernels:
            self.log.debug("Creating sub-kernel for %s", binding_name)
            binding = self.binding_manager.get(binding_name)
            self._kernels[binding_name] = spawn_kernel(
                self.kernel_manager,
                binding,
            )

        _, kc = self._kernels[binding_name]

        # TODO: add parent in here?
        msg = kc.session.msg("execute_request", content)
        self.log.debug("%s", msg)

        proxy = ProxyComms(self.session, parent, self.iopub_socket, self.shell_streams)

        kc.shell_channel.send(msg)
        kc.iopub_channel.pipe(proxy.on_iopub_message)
        kc.shell_channel.pipe(proxy.on_shell_message)

        # This will effectively block, but perhaps that is a good thing.
        # Without blocking, it seems to allow multiple cells to execute in parallel.
        while not proxy.reply_content:
            time.sleep(0.1)

        kc.iopub_channel.flush()
        kc.iopub_channel.unpipe()
        kc.shell_channel.unpipe()

        if not silent and proxy.reply_content["status"] == "error" and stop_on_error:
            yield self._abort_queues()
