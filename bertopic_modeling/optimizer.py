import mlflow
from mlflow.tracking import MlflowClient
from hyperopt import fmin, tpe, hp, STATUS_OK, Trials, partial, space_eval

import numpy as np
import pandas as pd
import tensorflow_text
import tensorflow as tf
import tensorflow_hub as hub

import umap
import hdbscan 
from nltk.corpus import stopwords

nltk.download('stopwords')

from bertopic import BERTopic
from bertopic.vectorizers import ClassTfidfTransformer
from bertopic.representation import KeyBERTInspired
from sklearn.feature_extraction.text import CountVectorizer


italian_stopwords = stopwords.words("italian")
np.random.seed(42)
_inf = np.finfo(np.float64).max

def create_ml_flow_experiment(client, experiment_name, experiment_tags):
        """
        Create a new mlflow experiment

        :client: MlFlow Client
        :experiment_name: Provide an Experiment unique name.
        :experiment_tags: Provide searchable tags that define characteristics of the Runs that will be in this Experiment.

        :return: new eval function.
        """
        if experiment_tags == None:
            # Provide an Experiment description that will appear in the UI
            experiment_description = (
                "This is short-text hard clustering evaluation using umap and hdbscan."
            )

            # Tags
            experiment_tags = {
                "project_name": "short-text-clustering",
                "dataset_description": "first-experiment",
                "mlflow.note.content": experiment_description,
            }

        # Create the Experiment, providing a unique name
        experiment = client.create_experiment(
            name=experiment_name, tags=experiment_tags
        )

        return experiment


def score_clusters(clusters, prob_threshold = 0.05):
    """
    Returns the label count and cost of a given cluster supplied from running hdbscan

    """

    cluster_labels = clusters.labels_
    label_count = len(np.unique(cluster_labels))
    total_num = len(clusters.labels_)
    cost = (np.count_nonzero(clusters.probabilities_ < prob_threshold)/total_num)

    return label_count, cost

def fit_BERTopic(sentences, best_mlflow_run): 

    # Step1 : Embedding
    embed = hub.load("https://tfhub.dev/google/universal-sentence-encoder-multilingual/3")
    # Step2 : Dimensionality Reduction
    umap_model = umap.UMAP(n_neighbors=int(best_mlflow_run.data.params['n_neighbors']), n_components=int(best_mlflow_run.data.params['n_components']), metric='cosine', min_dist=0.0, random_state=42)
    # Step3 : Clustering
    hdbscan_model = hdbscan.HDBSCAN(min_cluster_size=int(best_mlflow_run.data.params['min_cluster_size']), metric='euclidean', cluster_selection_method='eom', approx_min_span_tree=False, prediction_data=True)
    # Step4 : CountVectorizer
    # CountVectorizer and c-TF-IDF calculation are responsible for creating the topic representations
    # Fine-tune topic representations after training BERTopic
    vectorizer_model = CountVectorizer(stop_words=italian_stopwords, ngram_range=(1, 4), min_df=2)
    ctfidf_model = ClassTfidfTransformer()
    # Topic representations
    representation_model = KeyBERTInspired()
    topic_model = BERTopic(
                        embedding_model=embed,
                        umap_model=umap_model,
                        hdbscan_model=hdbscan_model,
                        vectorizer_model=vectorizer_model,
                        #ctfidf_model=ctfidf_model,
                        representation_model=representation_model)
    topics, probs = topic_model.fit_transform(sentences)
    return topics, probs, topic_model


def clustering_eval_mlflow(
        tracking_client, experiment_name, experiment_tags, run_name, sentences, label_lower=5, label_upper=10, penalty=0.3, max_evals = 100,
    ):

        """
        Create BERTtopic evaluation function with custom loss and constrained optimization of number of topics

        :experiment_id: Experiment unique name for the training run.
        :run_name: Define a run name for this iteration of training.
        :label_lower: Lower bound for K number of clusters.
        :label_upper: Upper bound for K number of clusters.
        :max_evals: Number of evals to train the model.


        :return: new eval function.
        """

        if tracking_client is None:
            print(f"Remember to configure an MLflow tracking server using command: mlflow server.\
                  By default --backend-store-uri is set to the local ./mlruns directory (the same as when running mlflow run locally),\
                  but when running a server, make sure that this points to a persistent (that is, non-ephemeral) file system location.")
            tracking_client = MlflowClient()

        exp = None
        if experiment_name is None:
            raise ValueError("Please ensure an Experiment Name which must be unique and case sensitive.")
        
        elif mlflow.get_experiment_by_name(experiment_name) is not None: 
            exp = mlflow.get_experiment_by_name(experiment_name).experiment_id
        else:
            exp = create_ml_flow_experiment(client=tracking_client, 
                                            experiment_name=experiment_name, 
                                            experiment_tags=experiment_tags)

        mlflow.set_experiment(experiment_id=exp)
        print("Experiment name set on MLFlow Tracking Server: \n", experiment_name)
        print("Experiment ID set on MLFlow Tracking Server: \n", exp)

        # Step1 : Embedding
        embed = hub.load("https://tfhub.dev/google/universal-sentence-encoder-multilingual/3")
        embeddings = embed(sentences)

        def objective_function_bert(params, eid, sentences , embeddings, label_lower, label_upper, penalty):
            """
            Objective function for hyperopt to minimize, which incorporates constraints
            on the number of clusters we want to identify
            """
            # Step2 : Dimensionality Reduction
            umap_model = umap.UMAP(n_neighbors=params['n_neighbors'], n_components=params['n_components'], metric='cosine', min_dist=0.0, random_state=params['random_state'])
            # Step3 : Clustering
            hdbscan_model = hdbscan.HDBSCAN(min_cluster_size=params['min_cluster_size'], metric='euclidean', cluster_selection_method='eom', approx_min_span_tree=False, prediction_data=True)
            topic_model = BERTopic(
                      umap_model=umap_model,
                      hdbscan_model=hdbscan_model)
            
            topics, probs = topic_model.fit_transform(sentences,np.array(embeddings))
            # Label count and cost associated with solution
            label_count, cost = score_clusters(topic_model.hdbscan_model, prob_threshold = 0.05)
            # % penalty on the cost function if outside the desired range of groups
            if (label_count < label_lower) | (label_count > label_upper):
                penalty = penalty
            else:
                penalty = 0

            loss = cost + penalty

            #Metrics
            umap_embeddings = np.array(topic_model.umap_model.embedding_, dtype=np.double)
            dbcv = hdbscan.validity_index(umap_embeddings,topic_model.hdbscan_model.labels_)
            metrics = {
                      'label_count': label_count,
                      'loss': loss,                     
                      'dbvc_score':dbcv
                      }

            mlflow.end_run()
            # Initiate the MLflow run context
            with mlflow.start_run(run_name=run_name) as run:
                # Log the parameters used for the model fit
                mlflow.log_params(params)
                # Log the error metrics that were calculated during validation
                mlflow.log_metrics(metrics)

            return {'loss': loss, 'label_count': label_count,'dbvc_score':dbcv, 'status':STATUS_OK}


        bopt_space = {
            "n_neighbors": hp.choice(('n_neighbors'),range(5,100)),
            "n_components": hp.choice(('n_components'),range(5,100)),
            "min_cluster_size": hp.choice(('min_cluster_size'),range(5,500)),
            "random_state":42,
        }

        fmin_objective = partial(objective_function_bert, 
                                 sentences=sentences, 
                                 eid=exp,
                                 embeddings= embeddings, 
                                 label_lower=label_lower, 
                                 label_upper=label_upper, 
                                 penalty=penalty)
        
        best = fmin(fmin_objective,
                    space = bopt_space,
                    algo=tpe.suggest,
                    max_evals=max_evals)

        best_params = space_eval(bopt_space, best)

        # find the best run, log its metrics as the final metrics of this run.

        runs = MlflowClient().search_runs(experiment_ids=exp,
                                          filter_string=f"tags.`mlflow.runName` = '{run_name}'")
        
        best_loss = _inf
        best_run = None
        for r in runs:
            if r.data.metrics["loss"] < best_loss:
                best_run = r
                best_loss = r.data.metrics["loss"]

        mlflow.set_tag("best_run", best_run.info.run_id)
        mlflow.set_tag("best params", str(best_params))

        """
        mlflow.log_metrics(
            {
                "Loss": best_run.data.metrics["loss"],
                "Label_count": best_run.data.metrics["label_count"],
                "Dbcv": best_run.data.metrics["dbvc_score"],
            }
        )
        """

        # Fit best BERTopic model
        topics, probs, topic_model = fit_BERTopic(sentences, best_run)

        return best_params, best_run, topic_model, topics

label_lower = 5
label_upper = 8
max_evals = 5
penalty= 0.3
run_name = "exp_umap_hdbscan_test_1"

tracking_client = MlflowClient(tracking_uri="http://127.0.0.1:5000")
experiment_name = "First-Dataset-Model-1"

# Provide an Experiment description that will appear in the UI
experiment_description = (
    "This is short-text hard clustering evaluation using umap and hdbscan."
)

# Tags
experiment_tags = {
    "project_name": "short-text-clustering",
    "dataset_description": "first-experiment",
    "mlflow.note.content": experiment_description,
}

# Create the Experiment, providing a unique name
experiment = tracking_client.create_experiment(
    name=experiment_name, tags=experiment_tags
)

df_json_survey = pd.read_json("C:\\Users\\Cecilia\\Downloads\\/1.json")

#tracking_client, experiment_name, experiment_tags, run_name, sentences, label_lower, label_upper, penalty, max_evals = 100
best_params, best_run, topic_model, topics = clustering_eval_mlflow(tracking_client = tracking_client, 
                                                experiment_tags=None,
                                                run_name = run_name, 
                                                sentences = list(df_json_survey['text']), 
                                                label_lower = label_lower, 
                                                label_upper = label_upper, 
                                                penalty = penalty, 
                                                max_evals = max_evals,
                                                experiment_name = experiment_name)

topic_model.visualize_barchart()
topic_model.get_topic_info()
topic_model.visualize_documents(list(df_json_survey['text']))
topic_model.visualize_topics()
topic_model.visualize_heatmap()
print(topics)