    def calc_imbalance(self, df):
        df['sum'] = df['bid_size'] + df['ask_size']
        df['time'] = df.index.astype(str)
        bids, asks = [], []
        for b, a in zip(df['bid_size'].astype(int).astype(str),
                        df['ask_size'].astype(int).astype(str)):
            dif = 4 - len(a)
            a = a + (' ' * dif)
            dif = 4 - len(b)
            b = (' ' * dif) + b
            bids.append(b)
            asks.append(a)

        df['text'] = pd.Series(bids, index=df.index) + '  ' + \
            pd.Series(asks, index=df.index)
        df.index = df['identifier']
        
        if self.imbalance_col is None:
            print("Calculating imbalance, as no imbalance column was provided.")
            df['size'] = (df['bid_size'] - df['ask_size'].shift().bfill()) / \
                (df['bid_size'] + df['ask_size'].shift().bfill())
            df['size'] = df['size'].ffill().bfill()
        else:
            print("Using imbalance column: {}".format(self.imbalance_col))
            df['size'] = df[self.imbalance_col]
            df = df.drop([self.imbalance_col], axis=1)
        # df = df.drop(['bid_size', 'ask_size'], axis=1)
        return df

    def annotate(self, df2):
