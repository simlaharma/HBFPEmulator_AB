# wide-resnet for cifar100
python main.py --type cnn --arch wideresnet28 --data cifar100 \
       --wideresnet_widen_factor 10 \
       --lr 0.1 --lr_decay True \
       --lr_decay_epochs 125,188 --num_epochs 250 \
       --use_nesterov True --momentum 0.9 --weight_decay 5e-4\
       --batch_size 128 \
       --avg_model True  --lr_warmup True \
       --num_workers 2 --eval_freq 1  --reshuffle_per_epoch True \
       --lr_lars False --lr_scale True \
       --world_size 2 --device gpu --save_all_models True
