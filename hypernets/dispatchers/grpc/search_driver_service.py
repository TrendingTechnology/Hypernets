import os
import pickle
import queue
import sys
import time
from threading import Thread

import grpc

from .proto import spec_pb2_grpc
from .proto.spec_pb2 import SearchResponse, PingMessage
from ...utils.common import config


class TrailItem(object):
    def __init__(self, trail_no, space_file, space_sample):
        super(TrailItem, self).__init__()

        self.trail_no = trail_no
        self.space_file = space_file
        self.space_sample = space_sample
        self.space_id = space_sample.space_id
        self.queue_at = time.time()

        self.success = False
        self.reward = float('nan')
        self.message = ''

    def __str__(self):
        return f'{self.__dict__}'


class SearchHolder(object):
    def __init__(self, search_id, spaces_dir, on_next, on_report, on_summary):
        super(SearchHolder, self).__init__()
        os.makedirs(spaces_dir, exist_ok=True)

        self.search_id = search_id
        self.spaces_dir = spaces_dir

        self.on_next = on_next
        self.on_report = on_report
        self.on_summary = on_summary

        self.start_at = time.time()
        self.finish_at = None

        self.queued_pool = queue.Queue()  # TrailItem
        self.running_items = {}  # space_id -> TrailItem
        self.reported_items = {}  # space_id -> TrailItem
        self.all_items = {}  # space_id -> TrailItem

    def add(self, trail_no, space_sample):
        space_id = space_sample.space_id
        assert space_id not in self.all_items.keys()

        space_file = f'{self.spaces_dir}/space-{trail_no}.pkl'
        with open(space_file, 'wb') as f:
            pickle.dump(space_sample, f)

        item = TrailItem(trail_no, space_file, space_sample)

        detail = f'trail_no={item.trail_no}, space_id={item.space_id}, space_file={space_file}'
        print(f'[{self.search_id}] [search]', detail)

        self.queued_pool.put(item)
        self.all_items[space_id] = item

    def readd(self, item):
        space_id = item.space_id
        space_file = item.space_file
        assert space_id in self.all_items.keys()

        detail = f'trail_no={item.trail_no}' \
                 + f', space_id={item.space_id}, space_file={space_file}'
        print(f'[{self.search_id}] [re-push]', detail)

        if space_id in self.running_items.keys():
            print(f'[remove running] {detail}')
            self.running_items.pop(space_id)

        if space_id in self.reported_items.keys():
            print(f'[remove reported] {detail}')
            self.reported_items.pop(space_id)

        self.queued_pool.put(item)

    def running_size(self):
        return len(self.all_items) - len(self.reported_items)

    def queue_size(self):
        return self.queued_pool.qsize()

    @property
    def running(self):
        return len(self.all_items) > len(self.reported_items)

    def get_next_item(self, peer, wait_hook):
        while self.running:
            try:
                item = self.queued_pool.get(False)
                item.peer = peer
                item.start_at = time.time()
                detail = f'trail_no={item.trail_no}, space_id={item.space_id}' \
                         + f',space_file={item.space_file}'
                print(f'[{self.search_id}] [dispatch] [{peer}]', detail)
                return item
            except queue.Empty:
                time.sleep(0.1)
                if wait_hook():
                    continue
                else:
                    break

        return None

    def report_item(self, peer, space_id, success, reward, message):
        assert space_id in self.all_items.keys()

        item = self.all_items[space_id]
        detail = f'trail_no={item.trail_no}, space_id={space_id}' \
                 + f', reward={reward}, success={success}'
        if not success:
            detail += f', message={message}'

        if space_id not in self.running_items.keys():
            msg = f'[{self.search_id}] [ignored-not running-report] [{peer}] {detail}'
            print(msg, file=sys.stderr)
        else:
            item.success = success
            item.reward = reward
            item.message = message
            item.report_at = time.time()

            self.running_items.pop(space_id)
            self.reported_items[space_id] = item

            print(f'[{self.search_id}] [report] [{peer}] {detail}')

            if self.on_report:
                try:
                    self.on_report(item)
                except Exception:
                    import traceback
                    traceback.print_exc()


class SearchDriverService(spec_pb2_grpc.SearchDriverServicer):
    def __init__(self, spaces_dir):
        super(SearchDriverService, self).__init__()

        self.spaces_dir = spaces_dir
        self.searches = []  # SearchHolder

        self.status_thread = DriverStatusThread(self)
        self.status_thread.start()

    def __del__(self):
        self.status_thread.stop()

    def _find_search(self, search_id):
        for s in self.searches:
            if s.search_id == search_id:
                return s
        return None

    def ping(self, request, context):
        peer = context.peer()
        message = request.message
        print(f'[ping] [{peer}] {message}')
        return PingMessage(message=message)

    def search(self, request_iterator, context):
        def response_with(search_id, item):
            return SearchResponse(code=SearchResponse.OK,
                                  search_id=search_id,
                                  trail_no=str(item.trail_no),
                                  space_id=item.space_id,
                                  space_file=item.space_file)

        def response_finished(search_id):
            return SearchResponse(code=SearchResponse.FINISHED,
                                  search_id=search_id,
                                  trail_no='',
                                  space_id='',
                                  space_file='')

        def response_waiting(search_id):
            return SearchResponse(code=SearchResponse.WAITING,
                                  search_id=search_id,
                                  trail_no='',
                                  space_id='',
                                  space_file='')

        def response_failed(search_id):
            return SearchResponse(code=SearchResponse.FAILED,
                                  search_id=search_id,
                                  trail_no='',
                                  space_id='',
                                  space_file='')

        peer = context.peer()
        running_item = None

        try:
            for request in request_iterator:
                search_id = request.search_id
                space_id = request.space_id

                search = self.current_search
                if search_id != search.search_id:
                    assert running_item is None

                    if self._find_search(search_id):
                        msg = response_finished(search_id)
                        yield msg
                        break
                    else:
                        # not found, maybe future search, make it wait
                        msg = response_waiting(search_id)
                        yield msg
                        continue

                if running_item:
                    assert space_id == running_item.space_id
                    success = request.success
                    reward = request.reward
                    message = request.message
                    search.report_item(peer, space_id, success, reward, message)
                    running_item = None

                item = search.get_next_item(peer, wait_hook=context.is_active)
                if item:
                    assert item.space_id not in search.running_items.keys()

                    msg = response_with(search_id, item)
                    search.running_items[item.space_id] = item
                    running_item = item
                    yield msg
                else:
                    break
        except grpc.RpcError as e:
            # ignore, just log it
            import traceback
            trace_detail = traceback.format_exc()
            try:
                msg = f'RpcError {peer} {e.__class__.__name__}: {e.code()}'
                print(msg, file=sys.stderr)
            except Exception:
                msg = f'RpcError {peer} {e.__class__.__name__}:\n'
                print(msg + trace_detail, file=sys.stderr)
        except Exception as e:
            print(f'{e.__class__.__name__}: {e}', file=sys.stderr)
            import traceback
            traceback.print_exc()
        finally:
            if running_item:
                self.current_search.readd(running_item)

    def start_search(self, search_id, on_next, on_report, on_summary):
        old_search = self._find_search(search_id)
        assert old_search is None

        search_spaces_dir = f'{self.spaces_dir}/{search_id}'
        search = SearchHolder(search_id, search_spaces_dir, on_next, on_report, on_summary)
        self.searches.append(search)
        print(f'>>>enter {search_id}')

    @property
    def current_search(self):
        assert len(self.searches) > 0
        return self.searches[-1]

    def add(self, trail_no, space_sample):
        self.current_search.add(trail_no, space_sample)

    def running_size(self):
        return self.current_search.running_size()

    def queue_size(self):
        return self.current_search.queue_size()


class DriverStatusThread(Thread):
    def __init__(self, service):
        super(DriverStatusThread, self).__init__()

        self.service = service
        self.daemon = True
        self.running = False
        self.summary_interval = float(config('summary_interval', '60'))

    def run(self) -> None:
        self.running = True

        summary_at = time.time()
        while self.running:
            try:
                now = time.time()
                if self.report_summary and self.summary_interval > 0 and summary_at + self.summary_interval < now:
                    self.report_summary()
                    summary_at = now

                time.sleep(1)
            except Exception:
                import traceback
                traceback.print_exc()

        print('SearchDriverService shutdown')

    def stop(self):
        self.running = False

    def report_summary(self):
        search = self.service.current_search
        search_id = search.search_id
        total = len(search.all_items)
        running = len(search.running_items)
        reported = len(search.reported_items)
        queued = total - running - reported

        msg = f'[{search_id}] [summary] queued={queued}, running={running}, reported={reported}, total={total}'
        if running > 0:
            detail = [f'trail-{x.trail_no}@{x.peer}' for x in search.running_items.values()]
            msg += '\n\trunning: ' + ','.join(detail)

        if search.on_summary:
            service_summary = search.on_summary()
            if service_summary:
                msg += '\n\t' + service_summary

        print(msg)


_grpc_servers = {}  # add -> tuple(grpc_server, driver_service)


def serve(addr, search_id, spaces_dir, on_next=None, on_report=None, on_summary=None):
    import grpc
    from concurrent import futures

    worker_number = int(config('grpc_worker_count', '10'))
    service = SearchDriverService(spaces_dir)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=worker_number))
    spec_pb2_grpc.add_SearchDriverServicer_to_server(service, server)

    server.add_insecure_port(addr)
    server.start()

    service.start_search(search_id, on_next, on_report, on_summary)

    return server, service


def get_or_serve(addr, search_id, spaces_dir, on_next=None, on_report=None, on_summary=None):
    s = _grpc_servers.get(addr)

    if s is None:
        server, service = serve(addr, search_id, spaces_dir, on_next, on_report, on_summary)
        _grpc_servers[addr] = (server, service)
    else:
        server, service = s
        service.start_search(search_id, on_next, on_report, on_summary)

    return server, service