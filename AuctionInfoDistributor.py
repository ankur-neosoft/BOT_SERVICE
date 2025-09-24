# AuctionInfoDistributor.py
import time
import zmq
import logging
from AucApi import get_all_live_auction_data
from config import auction_info_distributor_config

logger = logging.getLogger("AuctionInfoDistributor")
logging.basicConfig(level=logging.INFO)

def main():
    ctx = zmq.Context()
    socket = ctx.socket(zmq.PUB)
    port = auction_info_distributor_config.get('data_port', 9505)
    bind_addr = f"tcp://*:{port}"
    socket.bind(bind_addr)
    logger.info("Publisher bound to %s", bind_addr)

    poll_interval = auction_info_distributor_config.get('time_between_api_calls', 3)
    while True:
        try:
            resp = get_all_live_auction_data()
            if resp['status'] != 'success':
                logger.error("Get live auctions failed: %s", resp)
            else:
                snapshot = resp['message']
                # publish JSON snapshot (old code used socket.send_json)
                socket.send_json(snapshot)
                logger.debug("Published snapshot (len=%d)", len(snapshot))
        except Exception as e:
            logger.exception("Distributor loop error")
        time.sleep(poll_interval)

if __name__ == "__main__":
    main()
