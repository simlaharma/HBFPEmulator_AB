python main.py --type cnn --arch resnet20 --data cifar10 \
       --lr 0.1 --lr_decay True \
       --lr_decay_epochs 82,122 --num_epochs 160 \
       --use_nesterov True --momentum 0.9 --weight_decay 1e-4\
       --batch_size 128 \
       --avg_model True  --lr_warmup True \
       --num_workers 2 --eval_freq 1  --reshuffle_per_epoch True \
       --lr_lars False --lr_scale True \
       --world_size 2 --device gpu --save_some_models 40,80,120,158,159,160 --save_all_models True \
       --num_format bfp --rounding_mode stoc --mant_bits 3 --bfp_tile_size 8  \
       --weight_mant_bits 15 --manual_seed 1111 --mixed_precision 159,160 \
       --mixed_tile 8 --layer_mant 7