
## Folder Structure

Prepare the following folders to organize this repo:
```none
├── MMCTLN (code)
├── pretrain_weights (save the pretrained weights like vit, swin, etc)
├── model_weights (save the model weights)
├── fig_results (save the masks predicted by models)
├── lightning_logs (CSV format training logs)
├── data
│   ├── LoveDA
│   ├── uavid
│   ├── vaihingen
│   ├── potsdam 
```

## Install

Open the folder **airs** using **Linux Terminal** and create python environment:
```
conda create -n airs python=3.8
conda activate airs

conda install pytorch==1.10.0 torchvision==0.11.0 torchaudio==0.10.0 cudatoolkit=11.3 -c pytorch -c conda-forge
pip install -r GeoSeg/requirements.txt
```

## Pretrained Weights

[Quark Netdisk](https://pan.quark.cn/s/6ad115af5302) : rEsN

## Data Preprocessing

[Quark Netdisk](https://pan.quark.cn/s/dd067d024b07) : YxVA


## Training

```
python MMCTLN/train_supervision.py -c MMCTLN/config/uavid/***.py
```
Use different **config** to train different models.

## Validation

For example:
```
python MMCTLN/loveda_test.py -c MMCTLN/config/loveda/***.py -o fig_results/loveda/*** --rgb --val -t 'd4'
```

## Testing

**LoveDA**
```
python MMCTLN/loveda_test.py -c MMCTLN/config/loveda/***.py -o fig_results/loveda/*** -t 'd4'
```

**UAVid**
```
python MMCTLN/inference_uavid.py \
-i 'data/uavid/uavid_test' \
-c MMCTLN/config/uavid/***.py \
-o fig_results/uavid/*** \
-t 'lr' -ph 1152 -pw 1024 -b 2 -d "uavid"
```

## Inference on huge remote sensing image
```
python MMCTLN/inference_huge_image.py \
-i data/vaihingen/test_images \
-c GeoSeg/config/vaihingen/***.py \
-o fig_results/vaihingen/*** \
-t 'lr' -ph 512 -pw 512 -b 2 -d "pv"
```



## Reproduction Results
|    Method     |  Dataset  |  F1   |  OA   |  mIoU |
|:-------------:|:---------:|:-----:|:-----:|------:|
|  MMCTLN   | Vaihingen | 91.18 | 91.63 | 84.02 |
|  MMCTLN   |  Potsdam  | 93.37 | 91.95 | 87.77 |
|  MMCTLN   |  LoveDA   |   -   |   -   | 53.11 |
|  MMCTLN   |   UAVid   |   -   |   -   | 70.51 |


Due to some random operations in the training stage, reproduced results (run once) are slightly different from the reported in paper.



## Acknowledgement

Thanks to the GeoSeg framework for the help of the above code implementation of this work, and to the hardware support provided by the Advanced Computing Center of China Three Gorges University.
