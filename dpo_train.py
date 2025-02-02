# coding=utf-8
from typing import Dict, Optional
import time
import os

import pandas as pd
import torch
from datasets import Dataset, load_dataset
from transformers import PreTrainedTokenizerFast, TrainingArguments
from trl import DPOTrainer
from tokenizers import Tokenizer
from peft import LoraConfig, TaskType, PeftModel

from config import DpoConfig, T5ModelConfig
from model.chat_model import TextToTextModel
from utils.functions import get_T5_config

os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'

def get_dataset(split: str, file: str, cache_dir: str = '.cache') -> Dataset:
    """Load the Anthropic Helpful-Harmless dataset from Hugging Face and convert it to the necessary format.

    The dataset is converted to a dictionary with the following structure:
    {
        'prompt': List[str], 
        'chosen': List[str],
        'rejected': List[str],
    }
    """
    dataset = load_dataset('json', data_files=file,  split=split, cache_dir=cache_dir)

    def split_prompt_and_responses(sample: dict) -> Dict[str, str]:
        return {
            # add an eos token for signal that end of sentence, used in generate.
            "prompt": f"{sample['prompt']}[EOS]",  
            "chosen": f"{sample['chosen']}[EOS]",
            "rejected": f"{sample['rejected']}[EOS]",
        }

    return dataset.map(split_prompt_and_responses).shuffle(2333)


def train_dpo(config: DpoConfig, peft_config: LoraConfig=None) -> None:

    # step 1. Load tokenizer  
    tokenizer = PreTrainedTokenizerFast.from_pretrained(config.tokenizer_dir)
    
    # step 2. Load pre-trained model
    model_train, model_ref = None, None
    if os.path.isdir(config.sft_model_file):
        # Pass folder to from_pretrained 
        model_train = TextToTextModel.from_pretrained(config.sft_model_file)
        model_ref = TextToTextModel.from_pretrained(config.sft_model_file)
    else:
        # load_state_dict
        t5_config = get_T5_config(T5ModelConfig(), vocab_size=len(tokenizer), decoder_start_token_id=tokenizer.pad_token_id, eos_token_id=tokenizer.eos_token_id)

        model_train = TextToTextModel(t5_config)
        model_train.load_state_dict(torch.load(config.sft_model_file, map_location='cpu')) # set cpu for no exception

        model_ref = TextToTextModel(t5_config)
        model_ref.load_state_dict(torch.load(config.sft_model_file, map_location='cpu'))
    
    # 4. Load training dataset
    train_dataset = get_dataset("train", file=config.dpo_train_file)

    # 5. Load evaluation dataset 
    # eval_dataset = get_dataset("train", file=config.dpo_eval_file)
    eval_dataset = None

    # 6. Initialize training parameters
    training_args = TrainingArguments(
        per_device_train_batch_size=config.per_device_train_batch_size,
        num_train_epochs=config.num_train_epochs,
        auto_find_batch_size=True,
        remove_unused_columns=False,
        gradient_accumulation_steps=config.gradient_accumulation_steps,
        learning_rate=config.learning_rate,
        logging_first_step=True,
        logging_steps=config.logging_steps,
        save_steps=config.save_steps,
        output_dir=config.output_dir,
        optim="adafactor",
        report_to="tensorboard",
        log_level='info',
        warmup_steps=config.warmup_steps, 
        bf16=False,
        fp16=config.fp16, 
        seed=config.seed,
        logging_dir=config.log_dir, 
    )

    # 7. Initialize DPO trainer
    dpo_trainer = DPOTrainer(
        model_train, 
        model_ref,
        peft_config=peft_config,
        args=training_args, 
        beta=config.beta,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset, 
        tokenizer=tokenizer,
        max_length=config.max_seq_len, 
        max_target_length=config.max_seq_len,
        max_prompt_length=config.max_seq_len,
        generate_during_eval=True,
        is_encoder_decoder=True,
    )

    # 8. Train  
    dpo_trainer.train(
        # resume_from_checkpoint=True 
    )

    # 9. Save log  
    loss_log = pd.DataFrame(dpo_trainer.state.log_history)
    loss_log.to_csv(f"./logs/dpo_train_log_{time.strftime('%Y%m%d-%H%M')}.csv")
    
    # 10. Save model/lora
    suffixe = '/lora/' if peft_config is not None else '/dpo'
    model_save_dir = '/'.join(config.sft_model_file.split('/')[0: -1]) + suffixe

    dpo_trainer.save_model(model_save_dir)
    print(f'save model or lora adapter to: {model_save_dir}')

def merge_lora_weight_into_model(config: DpoConfig, peft_config: LoraConfig) -> None:

    # step 1. Load tokenizer
    tokenizer = PreTrainedTokenizerFast.from_pretrained(config.tokenizer_dir)
    
    # step 2. Load pre-trained model 
    sft_model = None
    if os.path.isdir(config.sft_model_file):
        # Pass folder to from_pretrained
        sft_model = TextToTextModel.from_pretrained(config.sft_model_file)
    else:
        # load_state_dict 
        t5_config = get_T5_config(T5ModelConfig(), vocab_size=len(tokenizer), decoder_start_token_id=tokenizer.pad_token_id, eos_token_id=tokenizer.eos_token_id)
        sft_model = TextToTextModel(t5_config)
        sft_model.load_state_dict(torch.load(config.sft_model_file, map_location='cpu')) # set cpu for no exception
        
    # Note this path needs to be consistent with 
    # model_save_dir in train_dpo function
    
        # 9. Save model/lora 
        # suffixe = '/lora/' if peft_config is not None else '/dpo'
        # model_save_dir = '/'.join(config.sft_model_file.split('/')[0: -1]) + suffixe

    adapter_save_dir = '/'.join(config.sft_model_file.split('/')[0: -1]) + '/lora'
    
    peft_model = PeftModel.from_pretrained( 
        model=sft_model, 
        model_id=adapter_save_dir, 
        config=peft_config, 
        adapter_name='adapter', 
    )
    
    # 3. Load adapter  
    print(f'Load adapter from dir: {adapter_save_dir}')

    peft_model.load_adapter(model_id=adapter_save_dir, adapter_name='adapter',)

    # 4. Merge  
    peft_model = peft_model.merge_and_unload()
    
    # 5. Save
    save_merge_file = config.sft_model_file + '.dpo_lora_merged'
    sft_model.save_pretrained(save_merge_file)
    print(f'Save merged model file to: {save_merge_file}')

   
if __name__ == "__main__":

    peft_config = LoraConfig(
         task_type=TaskType.SEQ_2_SEQ_LM, # text 2 text lora model   
         inference_mode=False,
         r=16,  
         lora_alpha=16,  
         lora_dropout=0.1,  
         bias="all", 
    )

    dpo_config = DpoConfig()

    # 1. Train
    train_dpo(dpo_config, peft_config=None)

    # 2. Merge lora adapter into model  
    # merge_lora_weight_into_model(dpo_config, peft_config)
