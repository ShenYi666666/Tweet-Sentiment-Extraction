cd ../src

# python train_v3.py train test3_roberta3 --batch-size 16 --train-file train_roberta.pkl --fold 0 --bert-path ../../bert_models/roberta_base/ --post --weight_decay 0.01 #--clean
# python train_v3.py train test3_roberta3 --batch-size 16 --train-file train_roberta.pkl --fold 1 --bert-path ../../bert_models/roberta_base/ --post --weight_decay 0.01
# python train_v3.py train test3_roberta3 --batch-size 16 --train-file train_roberta.pkl --fold 2 --bert-path ../../bert_models/roberta_base/ --post --weight_decay 0.01
# python train_v3.py train test3_roberta3 --batch-size 16 --train-file train_roberta.pkl --fold 3 --bert-path ../../bert_models/roberta_base/ --post --weight_decay 0.01
# python train_v3.py train test3_roberta3 --batch-size 16 --train-file train_roberta.pkl --fold 4 --bert-path ../../bert_models/roberta_base/ --post --weight_decay 0.01

# python train_v3.py validate test3_roberta3 --batch-size 16 --train-file train_roberta_v5.pkl --fold 0 --bert-path ../../bert_models/roberta_base/ --post #--clean
# python train_v3.py validate test3_roberta3 --batch-size 16 --train-file train_roberta_v5.pkl --fold 1 --bert-path ../../bert_models/roberta_base/ --post 
# python train_v3.py validate test3_roberta3 --batch-size 16 --train-file train_roberta_v5.pkl --fold 2 --bert-path ../../bert_models/roberta_base/ --post 
# python train_v3.py validate test3_roberta3 --batch-size 16 --train-file train_roberta_v5.pkl --fold 3 --bert-path ../../bert_models/roberta_base/ --post 
# python train_v3.py validate test3_roberta3 --batch-size 16 --train-file train_roberta_v5.pkl --fold 4 --bert-path ../../bert_models/roberta_base/ --post

python train_v3.py validate5 test3_roberta3 --batch-size 16 --train-file train_roberta_v5.pkl --fold 4 --bert-path ../../bert_models/roberta_base/ --post

# python train_v3.py predict5 test3_roberta3 --batch-size 16 --train-file train_roberta_v5.pkl --bert-path ../../bert_models/roberta_base/ --post

#### end #####