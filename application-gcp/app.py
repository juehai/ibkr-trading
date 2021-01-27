import logging
import os
import ib_insync
import pandas as pd
import numpy as np
import datetime as dt
import pytz
import time
import py_vollib
import py_vollib_vectorized

from google.cloud import datastore

logger = logging.getLogger(__name__)
ib_insync.util.logToConsole(level=logging.WARNING)

class Bot():
    def __init__(self, config):
        self.config = config
        
        host = config['IB_GATEWAY_HOST']
        port = config['IB_GATEWAY_PORT']

        self.ib = ib_insync.IB()
        self.ib.connect(
            host = host,
            port = port,
            clientId = config['CLIENT_ID'],
            timeout = 15,
            readonly = True)
        logging.info("Connected to IB on {}:{} mode.".format(host,port))

        self.ib.reqMarketDataType(config['MKT_DATA_TYPE'])

        self.datastore_client = datastore.Client()
        
        self.ptf = []
        self.options = []
        self.ptf_stats = {}
        self.pos_stats = {}

        # attempt to fix "competing live sessions"...
        time.sleep(2)

    
    def __del__(self):
        self.ib.disconnect()
        
    
    def get_state(self):
        """
        Get state
        """
        self.ptf = self.ib.portfolio()
            
    
    def process_options(self):
        """
        Parse options positions in the portfolio
        """
        self.options = [p.contract for p in self.ptf if p.contract.secType == 'OPT']
        self.options = self.ib.qualifyContracts(*self.options)
        self.tickers = self.ib.reqTickers(*self.options)
        
        # get underlying contracts and tickers
        underConIds = [self.ib.reqContractDetails(c)[0].underConId for c in self.options]
        underContracts = [ib_insync.Contract(conId=c) for c in underConIds]
        underContracts = self.ib.qualifyContracts(*underContracts)
        underTickers = self.ib.reqTickers(*underContracts)

        df_under = ib_insync.util.df(underTickers)
        df_under['price'] = df_under.apply(self.price_getter, axis=1)
        df_under = df_under[['contract', 'price']].add_suffix('_und')

        df = pd.concat([
            ib_insync.util.df(self.options),
            ib_insync.util.df(self.tickers),
            df_under
        ], axis=1)
  
        # greeks with pyvollib_vectorized
        df['risk_free_rate'] = self.config['INTEREST_RATE']
        df['right'] = df['right'].str.lower()
        df['option_price'] = df.apply(self.price_getter, axis=1)
        
        # get time to expiration in years
        expiration = pd.to_datetime(df['lastTradeDateOrContractMonth'] + 'T16:00:00',
                                    errors='raise').dt.tz_localize(tz='US/Eastern')
        df['dte'] = (expiration - dt.datetime.now(tz=pytz.timezone('US/Eastern'))) / pd.Timedelta('365 days')
        
        py_vollib_vectorized.price_dataframe(
            df,
            flag_col='right',
            underlying_price_col='price_und',
            strike_col='strike',
            annualized_tte_col='dte',
            riskfree_rate_col='risk_free_rate',
            price_col='option_price',
            model='black_scholes',
            inplace=True)
        
        # merge with portfolio to get positions
        ptf = ib_insync.util.df(self.ptf)
        ptf ['conId'] = ptf.apply(lambda x: x['contract'].conId,axis=1)

        df = ptf.merge(df, on='conId', suffixes=('','_y'), how='left')
        df.drop(df.filter(regex='_y$').columns.tolist(), axis=1, inplace=True)

        # position greeks
        df['theta'] = df['theta'] * np.sign(df['position'])
        df['delta'] = df['delta'] * np.sign(df['position'])
        df['vega'] = df['vega'] * np.sign(df['position'])
        df['gamma'] = df['gamma'] * np.sign(df['position'])
        df['rho'] = df['rho'] * np.sign(df['position'])
        
        self.options = df

    
    def portfolio_stats(self):
        """
        Calculate portfolio stats
        """
        df = self.options
        ptf_theta = np.sum(df['theta'] *
                           np.abs(df['position']) *
                           df['multiplier'].astype(np.float32))
        
        ptf_delta = np.sum(df['delta'] *
                           np.abs(df['position']) *
                           df['multiplier'].astype(np.float32) *
                           df['price_und'])

        self.ptf_stats['theta'] = ptf_theta
        self.ptf_stats['delta'] = ptf_delta
        

    def position_stats(self):
        """
        Determine position stats
        """
        df = self.options
        self.pos_stats = pd.pivot_table(
            df[df.secType == 'OPT'],
            values=['delta', 'theta', 'gamma', 'vega'],
            index=['symbol'],
            aggfunc='sum')
        
        
    def price_getter(self, obj):
        """
        Consistent price getter from ib_insync Ticker object
        """
        if obj.bid == -1.0 or obj.ask == -1.0:
            price = obj.close
        else:
            price = (obj.bid + obj.ask)/2.0
        assert (price >= 0), "Negative price"
        return price
        

    def save_state_to_datastore(self):
        """
        Save selected properties to datastore
        """
        
        key = self.datastore_client.key("Portfolio", "stats")
        entity = datastore.Entity(key=complete_key)
        entity.update(self.ptf_stats)
        self.datastore_client.put(entity)

    
    def run(self):
        """
        Routine to decide on the trade action
        """
        self.get_state()
        self.process_options()
        self.portfolio_stats()
        self.position_stats()
        self.save_state_to_datastore()
        

if __name__ == "__main__":
    config = {
        'TRADING_MODE': os.getenv('TRADING_MODE', 'paper'),
        'IB_GATEWAY_HOST': os.getenv('IB_GATEWAY_HOST', '127.0.0.1'),
        'IB_GATEWAY_PORT': os.getenv('IB_GATEWAY_PORT', 4001),
        'CLIENT_ID': os.getenv('CLIENT_ID', 1010),
        'MKT_DATA_TYPE': os.getenv('MKT_DATA_TYPE', 4),
        'INTEREST_RATE': 0.0025
    }
    
    bot = Bot(config)
    bot.run()