from ib_insync import *
ib = IB()
ib.connect('127.0.0.1', 4002, clientId=2)

# Search with no exchange
contract = Future('MES')
details = ib.reqContractDetails(contract)
for d in details:
    print(d.contract)

ib.disconnect()

# if there is another client id running in the background copy this code:  taskkill //F //IM python.exe 