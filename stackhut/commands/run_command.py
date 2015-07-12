# Copyright 2015 StackHut Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import subprocess
import uuid
import os
import sh

from stackhut import barrister
from stackhut import utils
from stackhut.utils import log, CloudStore, LocalStore
from stackhut import shim_server
from .commands import HutCmd
from .primitives import run_barrister, stacks

# Module Consts
REQ_FIFO = 'req.json'
RESP_FIFO = 'resp.json'

class RunCmd(HutCmd):
    """Base Run Command functionality"""
    def __init__(self, args):
        HutCmd.__init__(self, args)
        # setup the service contracts
        contract = barrister.contract_from_file(utils.CONTRACTFILE)
        self.server = barrister.Server(contract)
        # select the stack
        self.stack = stacks.get(self.hutcfg.stack)
        if self.stack is None:
            log.error("Unknown stack - {}".format(stack))
            exit(1)

    def run(self):
        super().run()

        # called by service on startup
        def _startup():
            log.debug('Starting up service')

            # startup the local helper service
            shim_server.init(self.store)

            # get/wait for a request
            try:
                in_str = self.store.get_request()
                input_json = json.loads(in_str)
            except:
                raise utils.ParseError()
            log.info('Input - \n{}'.format(input_json))

            def gen_id(d, v):
                d[v] = str(uuid.uuid4()) if v not in d else str(d[v])

            # massage the JSON-RPC request if we don't receive an entirely valid req
            default_service = 'Default'  # input_json['serviceName']
            gen_id(input_json, 'id')
            self.store.set_task_id(input_json['id'])

            def _make_json_rpc(req):
                if 'jsonrpc' not in req: req['jsonrpc'] = "2.0"
                gen_id(req, 'id')
                # add the default interface if none exists
                if req['method'].find('.') < 0:
                    req['method'] = "{}.{}".format(default_service, req['method'])
                return req

            if 'req' in input_json:
                reqs = input_json['req']
                if type(reqs) is list:
                    reqs = [_make_json_rpc(req) for req in reqs]
                else:
                    reqs = _make_json_rpc(reqs)
            else:
                raise utils.ParseError()

            os.remove(REQ_FIFO) if os.path.exists(REQ_FIFO) else None
            os.remove(RESP_FIFO) if os.path.exists(RESP_FIFO) else None
            os.mkfifo(REQ_FIFO)
            os.mkfifo(RESP_FIFO)

            self.stack.copy_shim()
            return reqs  # anything else

        # called by service on exit - clean the system, write all output data and return control back to docker
        # intended to upload all files into S#
        def _shutdown(res):
            log.info('Output - \n{}'.format(res))
            log.info('Shutting down service')
            # save output and log
            self.store.put_response(json.dumps(res))
            self.store.put_file(utils.LOGFILE)

        def _run_ext(method, params, req_id):
            """Make a pseudo-function call across languages"""
            # TODO - optimise
            # make dir to hold any output
            req_path = os.path.join(utils.STACKHUT_DIR, req_id)
            os.mkdir(req_path) if not os.path.exists(req_path) else None

            # create the req
            req = dict(method=method, params=params, req_id=req_id)

            # call out to sub process
            p = subprocess.Popen(self.stack.shim_cmd, shell=False, stderr=subprocess.STDOUT)
            # blocking-wait to send the request
            with open(REQ_FIFO, "w") as f:
                f.write(json.dumps(req))
            # blocking-wait to read the resp
            with open(RESP_FIFO, "r") as f:
                resp = json.loads(f.read())

            # now wait for completion
            # TODO - is this needed?
            p.wait()
            if p.returncode != 0:
                raise utils.NonZeroExitError(p.returncode, p.stdout)

            log.debug(resp)
            # basic error handling
            if 'error' in resp:
                code = resp['error']
                if code == barrister.ERR_METHOD_NOT_FOUND:
                    raise utils.ServerError(code, "Method or service {} not found".format(method))
                else:
                    raise utils.ServerError(code, resp['msg'])
            # return if no issue
            return resp['result']

        # Now run the main rpc commands
        try:
            reqs = _startup()
            resp = self.server.call(reqs, dict(callback=_run_ext))
            _shutdown(resp)
        except Exception as e:
            log.exception("Shit, unhandled error! - {}".format(e))
            exit(1)
        finally:
            # cleanup
            self.stack.del_shim()
            os.remove(REQ_FIFO)
            os.remove(RESP_FIFO)
            os.remove(utils.LOGFILE)

        # quit with correct exit code
        log.info('Service call complete')
        return 0


class RunLocalCmd(RunCmd):
    """"Concrete Run Command using local files for dev"""
    @staticmethod
    def parse_cmds(subparser):
        subparser = super(RunLocalCmd, RunLocalCmd).parse_cmds(subparser,
                                                               'run',
                                                               "Run StackHut service locally",
                                                               RunLocalCmd)
        subparser.add_argument("reqfile", default='test_request.json',
                               help="Test request file to use")
        subparser.add_argument("--container", '-c', type='store_true',
                               help="Run and test the service inside the container (requires you run build first)")

    def __init__(self, args):
        super().__init__(self, args)
        self.store = LocalStore(args.infile)

        self.infile = self.args.infile
        self.container = self.args.container

    def run(self):
        if self.container:
            tag = self.hutcfg.tag
            infile = os.path.abspath(self.infile)

            log.info("Running test service with {}".format(self.infile))
            # call docker to run the same command but in the container
            out = sh.docker.run('-v', '{}:/workdir/test_request.json:ro'.format(infile),
                                '--entrypoint=/usr/bin/stackhut', tag, '-vv', 'run', _out=lambda x: print(x, end=''))
            log.info("Finished test service")
        else:
            # make sure have latest idl
            run_barrister()
            super().run()



class RunCloudCmd(RunCmd):
    """Concrete Run Command using Cloud systems for prod"""
    @staticmethod
    def parse_cmds(subparser):
        subparser = super(RunCloudCmd, RunCloudCmd).parse_cmds(subparser,
                                                               'runcloud',
                                                               "(internal) Run a StackHut service",
                                                               RunCloudCmd)

    def __init__(self, args):
        RunCmd.__init__(self, args)
        self.store = CloudStore(self.hutcfg.name)

