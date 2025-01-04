import pandas as pd
df = pd.read_csv("tests/Upper Sevier (Draft5)_2023.csv")
print(df.loc[df['Unnamed: 0'] =='2023-04-01']['Otter Creek Feeder Canal'].values[0])